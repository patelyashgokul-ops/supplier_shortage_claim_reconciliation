"""Evidence revisions share the original evidence model and native attachment owner."""
import base64
import hashlib
import json
import math

from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from odoo.tools.mimetypes import guess_mimetype

from .claim_evidence import ClaimEvidence, SOURCES
from .claim_preparation import ROLE, lock_records

PHYSICAL = {'receipt','challan','packing','count','photo','return','quality','warehouse_report','lot'}
FINANCIAL = {'bill','bill_line','credit','account_note'}
MORE_TYPES = [('agreement','Supplier Agreement / Price Terms'),('warehouse_report','Warehouse Shortage Report'),
    ('lot','Lot / Serial Reference'),('bill_line','Vendor Bill Line'),('credit','Relevant Vendor Credit Note'),
    ('account_note','Accounting Review Note'),('supplier_confirmation','Supplier Written Confirmation'),
    ('supplier_response','Supplier Dispute Response'),('internal','Internal Verification Note')]
OUTPUTS = {'revision_root_id','supersedes_id','revision_number','is_current','current_revision_key','reviewed_fingerprint',
    'reviewed_basis','is_stale','rejection_reason','submitted_by','submitted_at','effective_state'}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()


class AdvancedEvidence(models.Model):
    _inherit = 'supplier.shortage.claim.evidence'

    evidence_type = fields.Selection(selection_add=MORE_TYPES,ondelete={key:'set other' for key,label in MORE_TYPES})
    verification_state = fields.Selection(selection_add=[('under_review','Under Review'),('requires_reverification','Requires Reverification')],
        ondelete={'under_review':'set not_reviewed','requires_reverification':'set not_reviewed'})
    scope_line_ids = fields.Many2many('supplier.shortage.claim.line','ssc_evidence_scope_rel','evidence_id','line_id',
        string='Explicit Supporting Lines',check_company=True,copy=False)
    statement = fields.Text(string='Human Verification Statement')
    revision_root_id = fields.Many2one('supplier.shortage.claim.evidence',readonly=True,ondelete='restrict',copy=False,index=True)
    supersedes_id = fields.Many2one('supplier.shortage.claim.evidence',readonly=True,ondelete='restrict',copy=False)
    revision_number = fields.Integer(readonly=True,default=1,copy=False)
    is_current = fields.Boolean(readonly=True,default=True,copy=False,index=True)
    current_revision_key = fields.Char(readonly=True,copy=False,index=True)
    change_reason = fields.Text(string='Revision Change Reason')
    reviewed_fingerprint = fields.Char(readonly=True,copy=False)
    reviewed_basis = fields.Json(readonly=True,copy=False)
    is_stale = fields.Boolean(readonly=True,default=False,copy=False,index=True)
    rejection_reason = fields.Text(readonly=True,copy=False)
    reviewer_id = fields.Many2one('res.users',string='Assigned Reviewer',check_company=True)
    submitted_by = fields.Many2one('res.users',readonly=True,copy=False)
    submitted_at = fields.Datetime(readonly=True,copy=False)
    effective_state = fields.Selection([('not_reviewed','Not Reviewed'),('under_review','Under Review'),('verified','Verified'),
        ('rejected','Rejected'),('requires_reverification','Requires Reverification'),('superseded','Superseded')],compute='_compute_effective')
    _current_unique = models.Constraint('UNIQUE(current_revision_key)','Only one current revision is permitted for a document.')
    _revision_unique = models.Constraint('UNIQUE(revision_root_id,revision_number)','Revision numbers must be unique within a document.')

    def _access_domain(self, operation):
        domain=super()._access_domain(operation)
        domain &= ~Domain('scope_line_ids','any!',~Domain('id','access','read'))
        return domain & (Domain('source_purchase_id','=',False) |
            ~Domain('claim_id.claim_line_ids','any!',~Domain('id','access','read')))

    def action_open_document(self):
        self.ensure_one()
        self.check_access('read')
        if self.statement:
            return {'type':'ir.actions.act_window','res_model':self._name,'res_id':self.id,'view_mode':'form','views':[(False,'form')]}
        return super().action_open_document()

    def _scope_lines(self):
        self.ensure_one()
        if self.claim_line_id:
            return self.claim_line_id
        if self.scope_line_ids:
            return self.scope_line_ids
        # Native PO identity is authoritative for every line of this single-PO claim.
        return self.claim_id._review_lines() if self.source_purchase_id else self.env['supplier.shortage.claim.line']

    def _require_edit(self):
        self.check_access('write')
        self.claim_id.check_access('read')
        lock_records(self.claim_id.purchase_id)
        lock_records(self.claim_id)
        self.claim_id.invalidate_recordset(['state'])
        if any(e.claim_id.state!='draft' or e.verification_state=='verified' or not e.is_current for e in self):
            raise ValidationError(self.env._('Verified, superseded or submitted documents are protected. Create a reasoned revision.'))
        for rec in self:
            if self.env.user.has_group(ROLE+'group_claim_user'):
                continue
            if rec.evidence_type in PHYSICAL and self.env.user.has_group(ROLE+'group_warehouse_user'):
                continue
            if rec.evidence_type in FINANCIAL and self.env.user.has_group(ROLE+'group_accounts_reviewer'):
                continue
            raise AccessError(self.env._('This evidence category is outside your contribution role.'))

    def _check_sources(self):
        super()._check_sources()
        for rec in self:
            scope = rec._scope_lines()
            scope.check_access('read')
            if any(line.claim_id!=rec.claim_id for line in scope) or rec.claim_line_id and rec.scope_line_ids and rec.scope_line_ids!=rec.claim_line_id:
                raise ValidationError(self.env._('All supporting lines must belong to this exact claim and line scope.'))
            if rec.source_purchase_line_id and any(line.purchase_line_id!=rec.source_purchase_line_id for line in scope):
                raise ValidationError(self.env._('A purchase-line reference supports only that purchase line.'))
            if rec.source_move_id and any(rec.source_move_id not in line.detection_id.source_move_ids for line in scope):
                raise ValidationError(self.env._('Movement evidence must support each exact selected purchase line.'))
            document=rec.source_bill_id or rec.source_bill_line_id.move_id
            if document and document.move_type!=('in_refund' if rec.evidence_type=='credit' else 'in_invoice'):
                raise ValidationError(self.env._('Distinguish Vendor Bills from Vendor Credit Notes in evidence classification.'))
            if rec.source_bill_id and any(line.purchase_line_id not in rec.source_bill_id.invoice_line_ids.purchase_line_id for line in scope):
                raise ValidationError(self.env._('The vendor document does not support every selected line.'))
            if rec.source_bill_line_id and any(line.purchase_line_id!=rec.source_bill_line_id.purchase_line_id for line in scope):
                raise ValidationError(self.env._('Bill-line evidence cannot support another PO line.'))
            if rec.source_picking_id and any(rec.source_picking_id not in line.detection_id.source_move_ids.picking_id for line in scope):
                raise ValidationError(self.env._('Receipt reference does not cover every selected line.'))

    @api.model_create_multi
    def create(self, vals_list):
        if any(OUTPUTS & values.keys() for values in vals_list):
            raise AccessError(self.env._('Revision and verification metadata are server controlled.'))
        prepared=[]
        with self.env.cr.savepoint():
            for values in vals_list:
                vals=dict(values)
                self.check_access('create')
                target=self.env['supplier.shortage.claim'].browse(vals.get('claim_id',self.env.context.get('default_claim_id')))
                target.check_access('read')
                if not target or target.state!='draft':
                    raise ValidationError(self.env._('Select an accessible draft claim before uploading evidence.'))
                if vals.get('upload_data'):
                    vals['upload_data']=self._encoded_upload(vals['upload_data'],target.company_id.ssc_evidence_max_mb*1024*1024)
                if vals.get('upload_data') and len(vals['upload_data'])>(target.company_id.ssc_evidence_max_mb*1024*1024*4//3+8):
                    raise ValidationError(self.env._('Evidence exceeds the company upload size limit.'))
                # Human statements remain text records, not fabricated receipt files.
                statement=vals.get('statement')
                if statement:
                    if vals.get('upload_data') or any(vals.get(field) for field in SOURCES) or vals.get('evidence_type') not in {'internal','account_note','warehouse_report'}:
                        raise ValidationError(self.env._('Use a standalone, clearly labelled internal statement.'))
                    if len(statement.strip())<15:
                        raise ValidationError(self.env._('Provide a substantive verification statement.'))
                if vals.get('upload_data'):
                    self._validate_file_content(vals['upload_data'],vals.get('filename',''))
                prepared.append(vals)
            records=super().create(prepared)
            for rec in records:
                super(ClaimEvidence,rec).write({'revision_root_id':rec.id,'current_revision_key':str(rec.id),
                    'submitted_by':self.env.uid,'submitted_at':fields.Datetime.now()})
                rec._audit_evidence('evidence_added')
                rec.claim_id._invalidate_evidence_readiness('Supporting evidence added')
            return records

    @api.model
    def _validate_file_content(self, encoded, filename):
        try:
            data=base64.b64decode(encoded,validate=True)
        except Exception:
            raise ValidationError(self.env._('Invalid supporting file content.')) from None
        ext=filename.rsplit('.',1)[-1].lower()
        detected=guess_mimetype(data,default='text/plain' if ext in {'txt','csv'} else 'application/octet-stream')
        safe={'pdf':{'application/pdf'},'jpg':{'image/jpeg'},'jpeg':{'image/jpeg'},'png':{'image/png'},
              'txt':{'text/plain'},'csv':{'text/plain','text/csv'}}
        if ext not in safe or detected not in safe[ext]:
            raise ValidationError(self.env._('File content does not match a supported passive business format. HTML, SVG and executable content are not permitted.'))
        if ext in {'txt','csv'}:
            try: text=data.decode('utf-8')
            except UnicodeDecodeError: raise ValidationError(self.env._('Text evidence must be valid UTF-8.')) from None
            if '\x00' in text or any(tag in text.lstrip().lower()[:256] for tag in ('<html','<!doctype','<svg','<script','<?xml')):
                raise ValidationError(self.env._('Active markup is not permitted as text evidence.'))

    def _basis(self):
        self.ensure_one()
        scope=self._scope_lines()
        details=[]
        for line in scope.sorted('id'):
            details.append([line.id,line.purchase_line_id.id,line.proposed_qty,line.claim_uom_id.id,line.claim_uom_id.factor,
                line.allocated_qty,line.allocation_state])
        source=[]
        for field in SOURCES:
            rec=self[field]
            if not rec: continue
            rec.check_access('read')
            if field=='source_purchase_id':
                source=[field,rec.id,rec.company_id.id,rec.partner_id.id,rec.state,rec.currency_id.id,
                    [[line.purchase_line_id.id,line.purchase_line_id.product_qty,line.purchase_line_id.uom_id.id,
                      line.purchase_line_id.price_unit,line.purchase_line_id.discount] for line in scope.sorted('id')]]
            elif field in ('source_bill_id','source_bill_line_id'):
                rows=rec.invoice_line_ids.filtered(lambda row:row.purchase_line_id in scope.purchase_line_id) if field=='source_bill_id' else rec
                source=[field,rec.id,[[row.id,row.move_id.state,row.move_id.move_type,row.quantity,row.product_uom_id.id,
                    row.price_unit,row.discount,row.price_subtotal,row.currency_id.id] for row in rows.sorted('id')]]
            else:
                source=[field,rec.id,fields.Datetime.to_string(rec.write_date)]
        return {'scope':details,'source':source,'attachment':self.attachment_id.checksum,
                'description':self.description,'statement':self.statement,'revision':self.revision_number}

    @api.depends('verification_state','reviewed_fingerprint','is_stale','is_current','scope_line_ids.proposed_qty',
        'scope_line_ids.allocation_state','claim_line_id.proposed_qty','attachment_id.checksum')
    @api.depends_context('uid','allowed_company_ids')
    def _compute_effective(self):
        for rec in self:
            state=rec.verification_state
            if not rec.is_current: state='superseded'
            elif state=='verified' and (rec.is_stale or not rec.reviewed_fingerprint or rec.verified_by==rec.create_uid
                    or rec.reviewed_fingerprint!=digest(rec._basis())):
                state='requires_reverification'
            elif state=='requires_more_evidence': state='requires_reverification'
            rec.effective_state=state

    def write(self, vals):
        if OUTPUTS & vals.keys():
            raise AccessError(self.env._('Historical revision and verification outputs are protected.'))
        if any(not rec.is_current for rec in self):
            raise ValidationError(self.env._('Superseded evidence is immutable.'))
        if vals.keys() & {'reviewer_id'}:
            if vals.keys()-{'reviewer_id'}:
                raise ValidationError(self.env._('Assign review separately from document changes.'))
            self._assert_reviewer()
            user=self.env['res.users'].browse(vals['reviewer_id'])
            user.check_access('read')
            if user and (not user.active or user.share or any(rec.company_id not in user.company_ids for rec in self)):
                raise ValidationError(self.env._('Assign an active internal reviewer for this company.'))
            super(ClaimEvidence,self).write(vals)
            self._audit_evidence('evidence_assigned')
            return True
        if vals.keys() & {'scope_line_ids','statement','change_reason'}:
            raise AccessError(self.env._('Change document scope or content through a new revision.'))
        result=super().write(vals)
        if vals.keys() & {'description'}:
            self.claim_id._invalidate_evidence_readiness('Evidence description changed')
        return result

    def _audit_evidence(self,event):
        for rec in self:
            if event=='evidence_added' and not rec.current_revision_key:
                continue
            super(AdvancedEvidence,rec)._audit_evidence(event)

    def _assert_reviewer(self):
        self.check_access('write')
        for rec in self:
            role='group_warehouse_reviewer' if rec.evidence_type in PHYSICAL else ('group_accounts_reviewer' if rec.evidence_type in FINANCIAL else 'group_claim_manager')
            if not self.env.user.has_group(ROLE+role):
                raise AccessError(self.env._('This evidence category needs the applicable warehouse, accounting or purchase review role.'))
            if rec.company_id not in self.env.companies:
                raise AccessError(self.env._('Activate an authorized review company.'))

    def _review(self,target):
        self._assert_reviewer()
        with self.env.cr.savepoint():
            self.claim_id.check_access('read')
            lock_records(self.claim_id.purchase_id)
            lock_records(self.claim_id)
            lock_records(self)
            self.invalidate_recordset()
            if any(rec.claim_id.state!='draft' or not rec.is_current or len((rec.verification_note or '').strip())<10 for rec in self):
                raise ValidationError(self.env._('Review only current draft evidence with a substantive conclusion.'))
            if target=='verified' and any(rec.create_uid==self.env.user for rec in self):
                raise AccessError(self.env._('An independent checker must verify evidence; uploaders cannot approve their own documents.'))
            if target=='verified' and any(not rec._scope_lines() for rec in self):
                raise ValidationError(self.env._('Specify the exact supporting line scope before verification.'))
            self._check_sources()
            for rec in self:
                if target=='verified' and rec.attachment_id:
                    self._validate_file_content(base64.b64encode(rec.attachment_id.raw.content),rec.filename or rec.attachment_id.name)
                basis=rec._basis()
                values={'verification_state':target,'verified_by':self.env.uid,'verified_at':fields.Datetime.now(),
                    'reviewed_fingerprint':digest(basis),'reviewed_basis':basis,'is_stale':False}
                if target=='rejected': values['rejection_reason']=rec.verification_note
                super(ClaimEvidence,rec).write(values)
                rec._audit_evidence('evidence_reviewed')
            self.claim_id._invalidate_evidence_readiness('Evidence review conclusion changed')
        return True

    def action_start_evidence_review(self):
        return self._review('under_review')

    def action_new_revision(self, values=None, reason=None):
        self.ensure_one()
        self.check_access('write')
        if values is None:
            wizard=self.env['supplier.shortage.evidence.revision.wizard'].create({'evidence_id':self.id,'scope_line_ids':[Command.set(self._scope_lines().ids)]})
            return {'type':'ir.actions.act_window','res_model':wizard._name,'res_id':wizard.id,'view_mode':'form','target':'new'}
        with self.env.cr.savepoint():
            self.claim_id.check_access('read')
            lock_records(self.claim_id.purchase_id)
            lock_records(self.claim_id)
            root=self.revision_root_id or self
            lock_records(root)
            self.invalidate_recordset()
            if self.claim_id.state!='draft' or not self.is_current or len((reason or '').strip())<10:
                raise ValidationError(self.env._('Revise only the current draft document, with a meaningful change reason.'))
            allowed={'upload_data','filename','description','statement','scope_line_ids'}
            if not isinstance(values,dict) or values.keys()-allowed:
                raise AccessError(self.env._('Revision input contains unsupported fields.'))
            vals={field:self[field].id for field in SOURCES if self[field]}
            vals.update(claim_id=self.claim_id.id,claim_line_id=self.claim_line_id.id,evidence_type=self.evidence_type,
                description=self.description,scope_line_ids=[Command.set(self.scope_line_ids.ids)],change_reason=reason)
            vals.update(values)
            if (self.attachment_id or self.statement) and not vals.get('upload_data') and not vals.get('statement'):
                raise ValidationError(self.env._('Upload the corrected file or statement; the original file is never overwritten.'))
            if not self.revision_root_id:
                super(ClaimEvidence,self).write({'revision_root_id':self.id,'current_revision_key':str(self.id)})
            super(ClaimEvidence,self).write({'is_current':False,'current_revision_key':False})
            if vals.get('statement') and len(vals['statement'].strip())<15:
                raise ValidationError(self.env._('Provide a substantive corrected statement.'))
            if vals.get('upload_data'):
                vals['upload_data']=self._encoded_upload(vals['upload_data'],self.company_id.ssc_evidence_max_mb*1024*1024)
                self._validate_file_content(vals['upload_data'],vals.get('filename',''))
            newer=super(AdvancedEvidence,self).create(vals)
            super(ClaimEvidence,newer).write({'revision_root_id':root.id,'supersedes_id':self.id,
                'revision_number':self.revision_number+1,'current_revision_key':str(root.id)})
            # Version the root even when it is no longer the current row.
            super(ClaimEvidence,root).write({'write_date':fields.Datetime.now()})
            newer._audit_evidence('evidence_revised')
            self.claim_id._invalidate_evidence_readiness('Document revised; a new independent review is required.')
            return {'type':'ir.actions.act_window','res_model':self._name,'res_id':newer.id,'view_mode':'form','views':[(False,'form')]}


class RevisionWizard(models.TransientModel):
    _name='supplier.shortage.evidence.revision.wizard'
    _description='Correct Supporting Evidence'
    evidence_id=fields.Many2one('supplier.shortage.claim.evidence',required=True)
    claim_id=fields.Many2one(related='evidence_id.claim_id',compute_sudo=False)
    scope_line_ids=fields.Many2many('supplier.shortage.claim.line','ssc_revision_wizard_scope_rel','wizard_id','line_id')
    upload_data=fields.Binary(attachment=False)
    filename=fields.Char()
    statement=fields.Text()
    change_reason=fields.Text()

    def action_create_revision(self):
        self.ensure_one()
        self.check_access('read')
        values={key:self[key] for key in ('upload_data','filename','statement') if self[key]}
        if self.upload_data:
            values['upload_data']=base64.b64encode(self.upload_data.content)
        values['scope_line_ids']=[Command.set(self.scope_line_ids.ids)]
        return self.evidence_id.action_new_revision(values,self.change_reason)
