import base64
import binascii
import mimetypes
import re

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError

from .claim_preparation import ROLE, require_claim_user, lock_records

EVIDENCE_TYPES = [('purchase','Purchase Order'),('receipt','Goods Receipt Note'),('bill','Vendor Bill'),
    ('challan','Delivery Challan'),('packing','Packing List'),('count','Warehouse Count Sheet'),
    ('photo','Shortage Photograph'),('supplier','Supplier Email / Letter'),('return','Supplier Return Document'),
    ('quality','Quality Inspection'),('other','Other Supporting Document')]
SOURCES = {'source_purchase_id':'purchase.order','source_picking_id':'stock.picking','source_bill_id':'account.move','source_purchase_line_id':'purchase.order.line','source_move_id':'stock.move','source_bill_line_id':'account.move.line'}
PROTECTED = {'company_id','attachment_id','verification_state','verified_by','verified_at'}


class ClaimEvidence(models.Model):
    _name = 'supplier.shortage.claim.evidence'
    _description = 'Claim Supporting Evidence'
    _check_company_auto = True
    _allow_sudo_commands = False
    _rec_name = 'description'

    claim_id = fields.Many2one('supplier.shortage.claim',required=True,check_company=True,ondelete='restrict',index=True)
    claim_line_id = fields.Many2one('supplier.shortage.claim.line',check_company=True,ondelete='restrict',index=True)
    company_id = fields.Many2one('res.company',related='claim_id.company_id',store=True,readonly=True,index=True)
    evidence_type = fields.Selection(EVIDENCE_TYPES,required=True)
    description = fields.Char(required=True)
    source_purchase_id = fields.Many2one('purchase.order',check_company=True,ondelete='restrict')
    source_picking_id = fields.Many2one('stock.picking',check_company=True,ondelete='restrict')
    source_bill_id = fields.Many2one('account.move',check_company=True,ondelete='restrict')
    source_purchase_line_id = fields.Many2one('purchase.order.line',check_company=True,ondelete='restrict')
    source_move_id = fields.Many2one('stock.move',check_company=True,ondelete='restrict')
    source_bill_line_id = fields.Many2one('account.move.line',check_company=True,ondelete='restrict')
    attachment_id = fields.Many2one('ir.attachment',readonly=True,ondelete='restrict',copy=False)
    upload_data = fields.Binary(string='Upload Supporting File',store=False)
    filename = fields.Char(string='File Name')
    verification_state = fields.Selection([('not_reviewed','Not Reviewed'),('verified','Internally Verified'),
        ('rejected','Rejected'),('requires_more_evidence','Requires More Evidence')],readonly=True,default='not_reviewed',copy=False)
    verified_by = fields.Many2one('res.users',readonly=True,copy=False,ondelete='restrict')
    verified_at = fields.Datetime(readonly=True,copy=False)
    verification_note = fields.Text(string='Evidence Review Note')

    def _access_domain(self, operation):
        domain = super()._access_domain(operation) & Domain('claim_id','access','read')
        if not any(self.env.user.has_group(ROLE+role) for role in ('group_claim_user','group_accounts_reviewer')):
            domain &= Domain('evidence_type','in',('purchase','receipt','challan','packing','count','photo','return','quality','warehouse_report','lot'))
        domain &= Domain('claim_line_id','=',False) | Domain('claim_line_id','access','read')
        for field in SOURCES:
            domain &= Domain(field,'=',False) | Domain(field,'access','read')
        return domain

    def _require_edit(self):
        self.check_access('write')
        self.claim_id.check_access('read')
        lock_records(self.claim_id.purchase_id)
        lock_records(self.claim_id)
        self.claim_id.invalidate_recordset(['state'])
        if any(rec.claim_id.state!='draft' or rec.verification_state=='verified' for rec in self):
            raise ValidationError(self.env._('Evidence is protected after verification or submission. Request more evidence or return the claim to draft; retain the original document.'))
        warehouse_only = not self.env.user.has_group(ROLE+'group_claim_user')
        if warehouse_only and (not self.env.user.has_group(ROLE+'group_warehouse_user') or any(
                rec.evidence_type not in ('receipt','challan','packing','count','photo','return','quality') for rec in self)):
            raise AccessError(self.env._('Warehouse evidence contributors may provide receiving evidence only.'))

    def _scope_lines(self):
        return self.claim_line_id or self.claim_id._review_lines()

    def _check_sources(self):
        self.check_access('read')
        for rec in self:
            rec.claim_id.check_access('read')
            if rec.claim_line_id and rec.claim_line_id.claim_id!=rec.claim_id:
                raise ValidationError(self.env._('Evidence line must belong to its claim.'))
            sources = self.env['purchase.order']
            for field in SOURCES:
                source = rec[field]
                if not source:
                    continue
                source.check_access('read')
                valid_types = {'source_purchase_id':('purchase','agreement'),'source_bill_id':('bill',),
                    'source_picking_id':('receipt','return','challan','quality'),
                    'source_purchase_line_id':('purchase','agreement'), 'source_move_id':('receipt','return','lot','warehouse_report'),
                    'source_bill_line_id':('bill_line','credit')}
                valid_types['source_bill_id']=('bill','credit')
                if rec.evidence_type not in valid_types[field]:
                    raise ValidationError(self.env._('Evidence type does not match the referenced Odoo document.'))
                if source.company_id!=rec.company_id:
                    raise ValidationError(self.env._('Evidence source company must match the claim.'))
                if field=='source_purchase_id' and source!=rec.claim_id.purchase_id:
                    raise ValidationError(self.env._('Reference the exact claim Purchase Order.'))
                lines = rec._scope_lines()
                candidates = lines.detection_id
                if field=='source_purchase_line_id' and source not in lines.purchase_line_id:
                    raise ValidationError(self.env._('Purchase-line evidence must match its exact scope.'))
                if field=='source_move_id' and source not in candidates.source_move_ids:
                    raise ValidationError(self.env._('Movement evidence must match verified receipt provenance.'))
                if field=='source_bill_line_id' and source not in candidates.source_bill_line_ids:
                    raise ValidationError(self.env._('Bill-line evidence must match exact purchase allocation.'))
                if field=='source_picking_id' and source not in candidates.source_move_ids.picking_id:
                    raise ValidationError(self.env._('Receipt evidence needs verified provenance from the selected claim lines.'))
                if field=='source_bill_id' and source not in candidates.source_bill_line_ids.move_id:
                    raise ValidationError(self.env._('Billing evidence must be attributable to these claim lines.'))
            if rec.attachment_id:
                rec.attachment_id.check_access('read')
                if rec.attachment_id.res_model!=rec._name or rec.attachment_id.res_id!=rec.id or rec.attachment_id.company_id!=rec.company_id:
                    raise ValidationError(self.env._('Evidence attachment must be privately owned by this evidence record.'))

    @api.model
    def _encoded_upload(self, value, maximum_bytes):
        # Odoo 20 accepts BinaryValue, base64 RPC strings and {content, filename}.
        # Retain encoded-byte compatibility for the existing preparation API.
        encoded=value.get('content') if isinstance(value,dict) else value
        if isinstance(encoded,(str,bytes)) and len(encoded)>(maximum_bytes*4//3+8):
            raise ValidationError(self.env._('Evidence exceeds the company upload size limit.'))
        if isinstance(value, bytes):
            return value
        try:
            binary=self._fields['upload_data'].convert_to_cache(value,self)
            if binary and binary.size>maximum_bytes:
                raise ValidationError(self.env._('Evidence exceeds the company upload size limit.'))
            return binary.to_base64() if binary else False
        except (TypeError,ValueError,binascii.Error):
            raise ValidationError(self.env._('Invalid supporting file content.')) from None

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access('create')
        if any(PROTECTED & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Attachment association and evidence verification are server controlled.'))
        with self.env.cr.savepoint():
            created = self.browse()
            for values in vals_list:
                vals = dict(values)
                for field in ('claim_id','claim_line_id','evidence_type','filename',*SOURCES):
                    if field not in vals and 'default_'+field in self.env.context:
                        vals[field]=self.env.context['default_'+field]
                upload = vals.pop('upload_data',None)
                if sum(bool(vals.get(field)) for field in SOURCES)+bool(upload)+bool(vals.get('statement'))!=1:
                    raise ValidationError(self.env._('Provide exactly one native source, private uploaded file or recorded human statement.'))
                claim = self.env['supplier.shortage.claim'].browse(vals.get('claim_id'))
                claim.check_access('read')
                if not claim:
                    raise ValidationError(self.env._('Select a claim.'))
                lock_records(claim.purchase_id)
                claim.lock_for_update()
                claim.invalidate_recordset(['state'])
                for field, model in SOURCES.items():
                    if vals.get(field):
                        self.env[model].browse(vals[field]).check_access('read')
                if vals.get('claim_line_id'):
                    self.env['supplier.shortage.claim.line'].browse(vals['claim_line_id']).check_access('read')
                vals.update(verification_state='not_reviewed',verified_by=False,verified_at=False,attachment_id=False)
                evidence = super(ClaimEvidence,self.with_context({k:v for k,v in self.env.context.items() if not k.startswith('default_')})).create(vals)
                evidence._require_edit()
                if upload:
                    upload=self._encoded_upload(upload,claim.company_id.ssc_evidence_max_mb*1024*1024)
                    if len(upload) > (claim.company_id.ssc_evidence_max_mb*1024*1024*4//3+8):
                        raise ValidationError(self.env._('The evidence file exceeds the company size limit.'))
                    try:
                        data = base64.b64decode(upload,validate=True)
                    except (ValueError,binascii.Error):
                        raise ValidationError(self.env._('The uploaded file is not valid base64 content.')) from None
                    filename = re.sub(r'[\x00-\x1f\x7f]','', (evidence.filename or '').replace('\\','/').split('/')[-1]).strip().lstrip('.')
                    extension = filename.rsplit('.',1)[-1].lower() if '.' in filename else ''
                    if len(filename)>255:
                        raise ValidationError(self.env._('Use a file name of at most 255 characters.'))
                    allowed = {item.strip().lower() for item in claim.company_id.ssc_evidence_extensions.split(',')}
                    if not data or len(data)>claim.company_id.ssc_evidence_max_mb*1024*1024 or extension not in allowed:
                        raise ValidationError(self.env._('Use a nonempty file within the company size limit and allowed extension policy.'))
                    attachment = self.env['ir.attachment']._create_evidence_file(evidence,{'name':filename,'raw':data,'type':'binary',
                        'mimetype':mimetypes.guess_type(filename)[0] or 'application/octet-stream',
                        'res_model':evidence._name,'res_id':evidence.id,'company_id':claim.company_id.id,'public':False})
                    super(ClaimEvidence,evidence).write({'attachment_id':attachment.id,'filename':filename})
                evidence._check_sources()
                evidence._audit_evidence('evidence_added')
                created |= evidence
            return created

    def write(self, vals):
        if vals.keys()-{'description','verification_note'}:
            raise AccessError(self.env._('Evidence source, content and verification metadata are immutable. Add a new evidence record for corrected documents.'))
        with self.env.cr.savepoint():
            if set(vals)=={'verification_note'} and self.env.user.has_group(ROLE+'group_claim_manager'):
                self.check_access('write')
                self.claim_id._lock_for_edit()
                if any(rec.claim_id.state!='draft' for rec in self):
                    raise ValidationError(self.env._('Return the claim to draft before recording evidence corrections.'))
            else:
                self._require_edit()
            changed = self.filtered(lambda rec:any(rec[key]!=value for key,value in vals.items()))
            result = super().write(vals)
            changed._audit_evidence('evidence_noted')
            return result

    def _audit_evidence(self,event):
        Audit = self.env['supplier.shortage.claim.audit']
        for rec in self:
            Audit._append_evidence(rec,event)

    def _review(self,target):
        require_claim_user(self.env,manager=True)
        with self.env.cr.savepoint():
            self.check_access('write')
            self.claim_id._lock_for_edit()
            lock_records(self)
            self.invalidate_recordset()
            if any(rec.claim_id.state!='draft' or len((rec.verification_note or '').strip())<5 for rec in self):
                raise ValidationError(self.env._('Review draft evidence with a meaningful verification note.'))
            self._check_sources()
            changed = self.filtered(lambda rec:rec.verification_state!=target)
            super(ClaimEvidence,changed).write({'verification_state':target,'verified_by':self.env.uid,'verified_at':fields.Datetime.now()})
            changed._audit_evidence('evidence_reviewed')
        return True

    def action_verify(self):
        return self._review('verified')

    def action_reject(self):
        return self._review('rejected')

    def action_request_more(self):
        return self._review('requires_more_evidence')

    def action_open_document(self):
        self.ensure_one()
        self.check_access('read')
        if self.attachment_id:
            self.attachment_id.check_access('read')
            return {'type':'ir.actions.act_url','url':'/web/content/%s?download=true'%self.attachment_id.id,'target':'self'}
        for field,model in SOURCES.items():
            if self[field]:
                self[field].check_access('read')
                if self[field].company_id!=self.company_id:
                    raise ValidationError(self.env._('The linked source company changed; obtain source revalidation.'))
                return {'type':'ir.actions.act_window','res_model':model,'res_id':self[field].id,'view_mode':'form','views':[(False,'form')],
                    'context':{'create':False,'edit':False,'delete':False}}

    def unlink(self):
        raise AccessError(self.env._('Evidence history is retained. Reject irrelevant documents rather than deleting them.'))

    def copy(self, default=None):
        raise AccessError(self.env._('Evidence records cannot be copied. Link an accessible original source or upload new evidence.'))
