"""Evidence-backed supplier positions; no settlement execution or recovery writes."""
import math
from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError
from .claim_preparation import ROLE
from .claim_submission import SUBMISSION, email_address, ClaimCommunication
from .evidence_verification import digest

RESPONSE = 'supplier.shortage.claim.response'


def authority(record, manager=False):
    record.check_access('read')
    claim = record.claim_id
    claim.check_access('write')
    if record.env.su or claim.company_id not in record.env.companies or claim.company_id not in record.env.user.company_ids:
        raise AccessError('Use an authenticated employee in the activated claim company.')
    roles = ('group_claim_manager', 'group_finance_manager') if manager else ('group_claim_user', 'group_finance_manager')
    if not any(record.env.user.has_group(ROLE + role) for role in roles):
        raise AccessError('Supplier response authority is required.')


def current_submission(sub):
    """Revalidate approval without conferring or requiring outbound email authority."""
    claim=sub.claim_id
    cycle=sub.approval_id
    if sub.state!='sent' or sub.superseded or cycle.state!='approved' or claim.approval_status!='approved' or cycle.number!=claim.approval_cycle:
        raise ValidationError('This response needs the current approved submitted revision; obtain reapproval and supplier confirmation.')
    claim._approval_ready()
    if digest(claim._approval_basis())!=cycle.fingerprint:
        raise ValidationError('The submitted approval basis changed; revalidate before accepting supplier decisions.')
    if cycle.snapshot['policy']['approval_chain']=='finance' and not sub.env.user.has_group(ROLE+'group_finance_manager'):
        raise AccessError('This approved scope requires Finance verification authority.')


def version_submission(sub):
    # Called only after authorization and locking; no commits or source writes.
    sub.env.cr.execute("UPDATE supplier_shortage_claim_submission SET write_date=clock_timestamp() AT TIME ZONE 'UTC' WHERE id=%s",[sub.id])
    sub.invalidate_recordset(['write_date'])


class SupplierResponse(models.Model):
    _name = RESPONSE
    _description = 'Supplier Decision on Submitted Revision'
    _check_company_auto = True
    _allow_sudo_commands = False
    _order = 'id desc'
    name = fields.Char(related='submission_id.name')
    submission_id = fields.Many2one(SUBMISSION, required=True, check_company=True, ondelete='restrict', index=True)
    claim_id = fields.Many2one(related='submission_id.claim_id', store=True, index=True)
    company_id = fields.Many2one(related='submission_id.company_id', store=True, index=True)
    supplier_id = fields.Many2one(related='submission_id.supplier_id')
    revision = fields.Integer(related='submission_id.revision')
    currency_id = fields.Many2one(related='submission_id.currency_id')
    approved_amount = fields.Monetary(related='submission_id.amount')
    supplier_contact_id = fields.Many2one('res.partner', required=True, check_company=True, ondelete='restrict')
    response_type = fields.Selection([('acknowledgment','Acknowledgment'), ('acceptance','Acceptance / Partial Acceptance'),
        ('rejection','Rejection / Partial Rejection'), ('evidence','Evidence Requested'), ('dispute','Disputed'), ('offer','Counteroffer')], required=True)
    response_source = fields.Selection([('email','Native Email Reply'), ('document','Supplier Document'),
        ('portal','Document / Portal Reference'), ('phone','Telephone'), ('meeting','In-person'), ('other','Other Documented Source')], required=True)
    response_date = fields.Datetime(required=True, default=fields.Datetime.now)
    response_reference = fields.Char()
    response_summary = fields.Text(required=True)
    verification_note = fields.Text()
    communication_id = fields.Many2one('supplier.shortage.claim.communication', readonly=True, ondelete='restrict')
    source_message_id = fields.Many2one('mail.message', ondelete='restrict')
    source_key = fields.Char(readonly=True, index=True)
    evidence_ids = fields.Many2many('supplier.shortage.claim.evidence', 'ssc_response_evidence_rel', 'response_id', 'evidence_id', check_company=True)
    line_ids = fields.One2many('supplier.shortage.response.line', 'response_id', copy=False)
    reported_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    reported_at = fields.Datetime(readonly=True)
    verification_state = fields.Selection([('unverified','Unverified'), ('review','Requires Review'), ('verified','Verified'),
        ('invalid','Rejected as Invalid'), ('superseded','Superseded')], default='unverified', required=True, readonly=True, index=True)
    verified_by = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    verified_at = fields.Datetime(readonly=True)
    superseded_by_id = fields.Many2one(RESPONSE, readonly=True, ondelete='restrict')
    current_key = fields.Char(readonly=True, index=True)
    position_kind = fields.Selection([('accepted','Full Acceptance'),('partial','Partial Acceptance'),('rejected','Full Rejection'),('mixed','Pending / Mixed')], compute='_positions', store=True, compute_sudo=False)
    accepted_amount = fields.Monetary(compute='_positions')
    rejected_amount = fields.Monetary(compute='_positions')
    disputed_amount = fields.Monetary(compute='_positions')
    pending_amount = fields.Monetary(compute='_positions')
    _source_unique = models.Constraint('UNIQUE(company_id, source_key)', 'This source message already has a response.')
    _current_unique = models.Constraint('UNIQUE(current_key)', 'Only one current position may cover a submitted revision.')

    def _access_domain(self, operation):
        return super()._access_domain(operation) & Domain('submission_id','access','read') & ~Domain('evidence_ids','any!',~Domain('id','access','read'))

    @api.depends('line_ids.accepted_amount','line_ids.rejected_amount','line_ids.disputed_amount','submission_id.amount')
    def _positions(self):
        for rec in self:
            rec.accepted_amount = sum(rec.line_ids.mapped('accepted_amount'))
            rec.rejected_amount = sum(rec.line_ids.mapped('rejected_amount'))
            rec.disputed_amount = sum(rec.line_ids.mapped('disputed_amount'))
            rec.pending_amount = rec.approved_amount - rec.accepted_amount - rec.rejected_amount - rec.disputed_amount
            rec.position_kind = 'accepted' if rec.approved_amount and rec.currency_id.is_zero(rec.approved_amount-rec.accepted_amount) else 'partial' if rec.accepted_amount>0 else 'rejected' if rec.approved_amount and rec.currency_id.is_zero(rec.approved_amount-rec.rejected_amount) else 'mixed'

    @api.model_create_multi
    def create(self, values):
        allowed = {'submission_id','supplier_contact_id','response_type','response_source','response_date','response_reference',
                   'response_summary','source_message_id','evidence_ids','line_ids'}
        result = self.browse()
        for value in values:
            if set(value) - allowed:
                raise AccessError('Response provenance and reviewer fields are server controlled.')
            sub = self.env[SUBMISSION].browse(value.get('submission_id'))
            sub.ensure_one(); sub.check_access('read'); authority(sub); sub._lock()
            if sub.state != 'sent':
                raise ValidationError('Responses require a sent submission revision.')
            message = self.env['mail.message'].browse(value.get('source_message_id'))
            key = False
            if message:
                message.check_access('read')
                if not message.message_id or message.model != SUBMISSION or message.res_id != sub.id:
                    raise ValidationError('The native source email must reference this exact submission and have a Message-ID.')
                key = message.message_id.strip()
                existing = self.search([('company_id','=',sub.company_id.id),('source_key','=',key)])
                if existing:
                    if existing.submission_id != sub:
                        raise ValidationError('This source already belongs to another revision.')
                    result |= existing
                    continue
            if any(command[0] != 0 for command in value.get('line_ids', [])):
                raise AccessError('Use new explicit positions; existing allocations cannot be reassigned.')
            commands=value.get('line_ids', [])
            identities=[command[2].get('claim_line_id') for command in commands]
            if len(set(identities))!=len(identities):
                raise ValidationError('One explicit position per exact submitted line.')
            clean = dict(value, source_key=key, reported_by=self.env.uid, reported_at=fields.Datetime.now())
            clean.pop('line_ids',None)
            rec = super(SupplierResponse, self.with_context({})).create(clean)
            # Explicit creation retains the authenticated actor. Odoo 20's
            # no-sudo relational command handler otherwise resets to transaction origin.
            if commands:
                self.env['supplier.shortage.response.line'].create([dict(command[2],response_id=rec.id) for command in commands])
            rec._validate_scope()
            kind = {'email':'email','document':'letter','phone':'phone'}.get(rec.response_source,'note')
            event = self.env['supplier.shortage.claim.communication']._append(sub,'inbound',kind,rec.response_summary,message,
                partner=rec.supplier_contact_id,source_reference=message.message_id if message else False)
            rec._set({'communication_id':event.id})
            version_submission(sub)
            sub._audit('Supplier response %s recorded, unverified; no recovery.' % rec.id,rec.response_summary)
            result |= rec
        return result

    def write(self, values):
        if set(values) != {'verification_note'}:
            raise AccessError('Supplier responses are immutable. Record a revised response or use review actions.')
        for rec in self:
            authority(rec,True); rec.submission_id._lock()
            if rec.verification_state not in ('unverified','review'):
                raise AccessError('Verified source review is immutable.')
        return super().write(values)

    def unlink(self):
        raise AccessError('Supplier response history must be retained.')

    def copy(self, default=None):
        raise AccessError('Record a distinct revised supplier response.')

    def _set(self, values):
        return super().write(values)

    def _validate_scope(self):
        self.ensure_one()
        contact = self.supplier_contact_id
        contact.check_access('read')
        if contact.commercial_partner_id != self.supplier_id.commercial_partner_id:
            raise ValidationError('Select a contact belonging to the submitted supplier.')
        if len((self.response_summary or '').strip()) < 20:
            raise ValidationError('Provide a source and discussion summary of at least 20 characters.')
        if self.response_date > fields.Datetime.now():
            raise ValidationError('A received response cannot be dated in the future.')
        for evidence in self.evidence_ids:
            evidence.check_access('read')
            if evidence.claim_id != self.claim_id or evidence.company_id != self.company_id:
                raise ValidationError('Response evidence must belong to this claim and company.')
            if evidence.supplier_response_submission_id and evidence.supplier_response_submission_id!=self.submission_id:
                raise ValidationError('Supplier evidence belongs to a different submitted revision.')
            if evidence.attachment_id:
                evidence.attachment_id.check_access('read')
        self.line_ids._validate_position()
        if len(self.line_ids.claim_line_id) != len(self.line_ids):
            raise ValidationError('One explicit position per exact claim line.')
        if self.response_type in ('acknowledgment','evidence','offer') and self.line_ids:
            raise ValidationError('Acknowledgments, evidence requests and offers cannot allocate acceptance.')

    def action_verify(self):
        self.ensure_one(); authority(self,True)
        self.submission_id._lock(); self.invalidate_recordset()
        if self.verification_state == 'verified':
            return True
        if self.verification_state not in ('unverified','review'):
            raise ValidationError('This historical response cannot become effective again.')
        self._validate_scope()
        if self.reported_by == self.env.user or len((self.verification_note or '').strip()) < 20:
            raise ValidationError('An independent reviewer and substantive verification note are required.')
        sub = self.submission_id
        # Preserve STEP 12 approval authority and material-change revalidation.
        current_submission(sub)
        if self.response_source == 'email':
            msg = self.source_message_id
            if not msg or msg.message_type != 'email' or msg.model != SUBMISSION or msg.res_id != sub.id or not sub.message_id or msg.parent_id != sub.message_id:
                raise ValidationError('Use an original native reply to this exact outgoing submission message.')
            if email_address(msg.email_from or '') != email_address(self.supplier_contact_id.email or ''):
                raise ValidationError('Unexpected sender: requires additional supplier identity verification.')
            if not self.evidence_ids:
                raise ValidationError('Native threading is correlation, not authentication; independently verified supplier evidence is required.')
        elif self.response_source in ('document','portal','other'):
            if not self.evidence_ids or not self.response_reference:
                raise ValidationError('Documented sources require supplier evidence and an original reference.')
        elif not self.response_reference:
            raise ValidationError('Record a contact/discussion reference for phone or meeting review.')
        for evidence in self.evidence_ids:
            if evidence.evidence_type not in ('supplier','supplier_confirmation','supplier_response') or evidence.effective_state != 'verified':
                raise ValidationError('Link current independently verified supplier evidence; filenames and internal notes alone are insufficient.')
        if self.response_type in ('acceptance','rejection','dispute') and not self.line_ids:
            raise ValidationError('A decision requires explicit line positions.')
        if self.response_type == 'acceptance' and not any(self.line_ids.mapped('accepted_qty')):
            raise ValidationError('Acceptance requires positive accepted quantity.')
        if self.response_type == 'rejection' and not any(self.line_ids.mapped('rejected_qty')):
            raise ValidationError('Rejection requires positive rejected quantity.')
        old = self.search([('current_key','=',str(sub.id))])
        if self.line_ids:
            previous={line.claim_line_id.id:line.accepted_qty for line in old.line_ids}
            incoming={line.claim_line_id.id:line.accepted_qty for line in self.line_ids}
            reduction=any(incoming.get(key,0)<qty for key,qty in previous.items())
            if old and reduction and not self.env.user.has_group(ROLE+'group_finance_manager'):
                raise AccessError('Reducing a verified acceptance requires Finance review.')
            if old:
                old._set({'current_key':False,'verification_state':'superseded','superseded_by_id':self.id})
            self._set({'current_key':str(sub.id)})
        self._set({'verification_state':'verified','verified_by':self.env.uid,'verified_at':fields.Datetime.now()})
        # Version the parent: stale Repeatable Read transactions must retry.
        version_submission(sub)
        sub._audit('Response %s verified; prior position %s. Accepted %s, rejected %s, disputed %s, pending %s; recovery unchanged.' %
                   (self.id,old.id or '-',self.accepted_amount,self.rejected_amount,self.disputed_amount,self.pending_amount),self.verification_note)
        return True

    def action_require_review(self):
        self.ensure_one(); authority(self,True); self.submission_id._lock()
        if self.verification_state not in ('unverified','review'):
            raise ValidationError('Effective decisions require a revised response.')
        if self.verification_state != 'review':
            self._set({'verification_state':'review'})
            self.submission_id._audit('Response %s requires further source review.' % self.id)

    def action_invalidate(self):
        self.ensure_one(); authority(self,True); self.submission_id._lock()
        if self.verification_state not in ('unverified','review') or len((self.verification_note or '').strip())<20:
            raise ValidationError('Only unverified responses can be invalidated, with a substantive reason.')
        self._set({'verification_state':'invalid','verified_by':self.env.uid,'verified_at':fields.Datetime.now()})
        self.submission_id._audit('Response %s rejected as invalid.' % self.id,self.verification_note)


class ResponseLine(models.Model):
    _name = 'supplier.shortage.response.line'
    _description = 'Non-overlapping Supplier Line Position'
    _check_company_auto = True
    _allow_sudo_commands = False
    response_id = fields.Many2one(RESPONSE, required=True, check_company=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='response_id.company_id', store=True, index=True)
    claim_line_id = fields.Many2one('supplier.shortage.claim.line', required=True, check_company=True, ondelete='restrict')
    currency_id = fields.Many2one(related='response_id.currency_id')
    product_id = fields.Many2one(related='claim_line_id.product_id')
    purchase_line_id = fields.Many2one(related='claim_line_id.purchase_line_id')
    submitted_qty = fields.Float(compute='_amounts', digits='Product Unit')
    product_uom_id = fields.Many2one('uom.uom', compute='_amounts')
    accepted_qty = fields.Float(digits='Product Unit')
    rejected_qty = fields.Float(digits='Product Unit')
    disputed_qty = fields.Float(digits='Product Unit')
    pending_qty = fields.Float(compute='_amounts', digits='Product Unit')
    accepted_amount = fields.Monetary(compute='_amounts')
    rejected_amount = fields.Monetary(compute='_amounts')
    disputed_amount = fields.Monetary(compute='_amounts')
    decision_reason = fields.Text()
    rejection_reason_id = fields.Many2one('supplier.shortage.response.reason', ondelete='restrict')
    _line_unique = models.Constraint('UNIQUE(response_id, claim_line_id)', 'One position per exact line and response.')

    def _snapshot(self):
        self.ensure_one()
        rows = [r for r in self.response_id.submission_id.external_snapshot['lines'] if r['line_id'] == self.claim_line_id.id]
        if len(rows) != 1:
            raise ValidationError('The exact claim line is absent from this submitted revision.')
        return rows[0]

    @api.depends('accepted_qty','rejected_qty','disputed_qty','response_id.submission_id','claim_line_id')
    def _amounts(self):
        for row in self:
            snap = row._snapshot() if row.response_id and row.claim_line_id else {}
            qty = snap.get('quantity',0)
            row.submitted_qty = qty
            row.product_uom_id = snap.get('uom_id')
            row.pending_qty = qty-row.accepted_qty-row.rejected_qty-row.disputed_qty
            unit = snap.get('amount',0)/qty if qty else 0
            row.accepted_amount = unit*row.accepted_qty
            row.rejected_amount = unit*row.rejected_qty
            row.disputed_amount = unit*row.disputed_qty

    def _validate_position(self):
        for row in self:
            row.claim_line_id.check_access('read')
            snap = row._snapshot()
            quantities = (row.accepted_qty,row.rejected_qty,row.disputed_qty)
            if not all(math.isfinite(q) and q>=0 for q in quantities):
                raise ValidationError('Position quantities must be finite and nonnegative.')
            if sum(quantities)>snap['quantity'] or self.env['uom.uom'].browse(snap['uom_id']).compare(sum(quantities),snap['quantity'])>0:
                raise ValidationError('Allocated positions exceed the submitted quantity.')
            if row.rejected_qty and (not row.rejection_reason_id or len((row.decision_reason or '').strip())<20):
                raise ValidationError('Rejected quantities require a controlled reason and substantive explanation.')

    @api.model_create_multi
    def create(self, values):
        allowed = {'response_id','claim_line_id','accepted_qty','rejected_qty','disputed_qty','decision_reason','rejection_reason_id'}
        for value in values:
            if set(value)-allowed:
                raise AccessError('Submitted quantities and monetary attribution come from the frozen revision.')
            try:
                valid=all(math.isfinite(float(value.get(f,0))) and float(value.get(f,0))>=0 for f in ('accepted_qty','rejected_qty','disputed_qty'))
            except (TypeError,ValueError,OverflowError):
                valid=False
            if not valid:
                raise ValidationError('Position quantities must be finite and nonnegative.')
            parent = self.env[RESPONSE].browse(value.get('response_id'))
            authority(parent); parent.submission_id._lock()
            if parent.verification_state != 'unverified' or parent.communication_id:
                raise AccessError('Positions are frozen on recording. Create a revised response.')
        records = super(ResponseLine,self.with_context({})).create(values)
        records._validate_position()
        return records

    def write(self, values):
        raise AccessError('Line positions are immutable; record a revised response.')

    def unlink(self):
        raise AccessError('Supplier decision allocations are retained.')

    def copy(self, default=None):
        raise AccessError('Supplier decision allocations cannot be copied.')


class ResponseReason(models.Model):
    _name = 'supplier.shortage.response.reason'
    _description = 'Supplier Rejection Reason'
    name = fields.Char(required=True)
    active = fields.Boolean(default=True)

    def _configure(self):
        if self.env.su or not self.env.user.has_group(ROLE+'group_module_admin'):
            raise AccessError('Only a configuration administrator maintains reason codes.')

    @api.model_create_multi
    def create(self, values):
        self._configure()
        return super(ResponseReason,self.with_context({})).create(values)

    def write(self, values):
        self._configure()
        return super().write(values)

    def unlink(self):
        raise AccessError('Archive reason codes to preserve decisions.')
