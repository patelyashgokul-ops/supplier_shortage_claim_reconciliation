"""Supplier documents reuse the protected evidence/file/reviewer architecture."""
from contextvars import ContextVar
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from .claim_submission import SUBMISSION
from .supplier_response import authority
from .claim_preparation import lock_records
from .claim_evidence import ClaimEvidence

SUPPLIER_DOCUMENT = ContextVar('ssc_supplier_response_document',default=False)


class SupplierResponseEvidence(models.Model):
    _inherit = 'supplier.shortage.claim.evidence'
    supplier_response_submission_id = fields.Many2one(SUBMISSION,check_company=True,ondelete='restrict',index=True,copy=False,
        help='Supplier-origin response evidence, separate from the approved outgoing claim package.')

    @api.model_create_multi
    def create(self, values):
        bound=[v.get('supplier_response_submission_id') for v in values]
        if not any(bound):
            # Context defaults must never manufacture a supplier-document capability.
            if self.env.context.get('default_supplier_response_submission_id'):
                raise AccessError('Select the supplier response submission explicitly.')
            return super().create(values)
        if not all(bound):
            raise ValidationError('Record supplier response documents separately from claim preparation evidence.')
        for v in values:
            sub=self.env[SUBMISSION].browse(v['supplier_response_submission_id']); authority(sub); sub._lock()
            if sub.state!='sent' or v.get('claim_id')!=sub.claim_id.id or v.get('evidence_type') not in ('supplier','supplier_confirmation','supplier_response') or not v.get('upload_data'):
                raise ValidationError('Upload an actual supplier document against its exact sent claim revision.')
            if not v.get('scope_line_ids'):
                raise ValidationError('Supplier documents require explicit claim line scope.')
        token=SUPPLIER_DOCUMENT.set(True)
        try:
            records=super(SupplierResponseEvidence,self.with_context({})).create(values)
            for rec in records:
                valid={r['line_id'] for r in rec.supplier_response_submission_id.external_snapshot['lines']}
                if set(rec.scope_line_ids.ids)-valid:
                    raise ValidationError('Supplier document scope must belong to this submitted revision.')
            return records
        finally:
            SUPPLIER_DOCUMENT.reset(token)

    def _require_edit(self):
        bound=self.filtered('supplier_response_submission_id')
        if not bound:
            return super()._require_edit()
        if bound!=self:
            raise ValidationError('Edit supplier response evidence separately.')
        self.check_access('write')
        for rec in self:
            authority(rec.supplier_response_submission_id); rec.supplier_response_submission_id._lock()
            lock_records(rec)
            if not rec.is_current or rec.verification_state=='verified':
                raise AccessError('Verified supplier documents are immutable.')

    def write(self, values):
        if 'supplier_response_submission_id' in values:
            raise AccessError('Supplier evidence provenance cannot be changed.')
        if self.filtered('supplier_response_submission_id'):
            if set(values)-{'verification_note'}:
                raise AccessError('Supplier response files and scope are immutable.')
            self._assert_reviewer(); self._require_edit()
            result=super(ClaimEvidence,self).write(values)
            self._audit_evidence('evidence_noted')
            return result
        return super().write(values)

    def _review(self, target):
        if self and all(self.mapped('supplier_response_submission_id')):
            token=SUPPLIER_DOCUMENT.set(True)
            try:
                return super()._review(target)
            finally:
                SUPPLIER_DOCUMENT.reset(token)
        return super()._review(target)

    def action_flag_claim_contradiction(self):
        self.ensure_one(); authority(self.supplier_response_submission_id,True)
        self.supplier_response_submission_id._lock()
        if not self.supplier_response_submission_id or len((self.verification_note or '').strip())<20:
            raise ValidationError('Document the contradictory source and required review.')
        self.claim_id._invalidate_evidence_readiness('Supplier evidence contradicts the submitted claim; source review and reapproval required.')
        self.supplier_response_submission_id._audit('Supplier evidence %s flagged as contradictory; approval invalidated. Sources unchanged.' % self.id,self.verification_note)

    def action_new_revision(self, values=None, reason=None):
        if self.filtered('supplier_response_submission_id'):
            raise AccessError('Retain the original supplier document and upload new response evidence with its own source.')
        return super().action_new_revision(values,reason)


class SupplierDocumentClaim(models.Model):
    _inherit = 'supplier.shortage.claim'

    def _invalidate_evidence_readiness(self, reason):
        if SUPPLIER_DOCUMENT.get():
            return
        return super()._invalidate_evidence_readiness(reason)
