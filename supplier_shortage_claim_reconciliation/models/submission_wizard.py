from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError
from .claim_submission import SUBMISSION


class SupplierSendWizard(models.TransientModel):
    _name='supplier.shortage.claim.send.wizard'
    _description='Review Exact Supplier Claim Package'
    submission_id=fields.Many2one(SUBMISSION,required=True,ondelete='cascade')
    claim_id=fields.Many2one(related='submission_id.claim_id')
    company_id=fields.Many2one(related='submission_id.company_id')
    supplier_id=fields.Many2one(related='submission_id.supplier_id')
    supplier_commercial_id=fields.Many2one(related='submission_id.supplier_id.commercial_partner_id')
    approval_id=fields.Many2one(related='submission_id.approval_id')
    currency_id=fields.Many2one(related='submission_id.currency_id')
    amount=fields.Monetary(related='submission_id.amount')
    recipient_partner_id=fields.Many2one('res.partner')
    recipient_email=fields.Char()
    cc_partner_ids=fields.Many2many('res.partner','ssc_send_wizard_cc_rel','wizard_id','partner_id')
    subject=fields.Char(required=True)
    body_html=fields.Html(required=True,sanitize=True)
    evidence_ids=fields.Many2many('supplier.shortage.claim.evidence','ssc_send_wizard_evidence_rel','wizard_id','evidence_id')
    attachment_ids=fields.Many2many(related='submission_id.attachment_ids')
    preview_fingerprint=fields.Char(readonly=True)
    confirmed=fields.Boolean(string='I reviewed the exact PDF, recipients, message and attachments')

    @api.onchange('recipient_partner_id')
    def _onchange_recipient(self):
        self.recipient_email=self.recipient_partner_id.email or False

    @api.model_create_multi
    def create(self, values):
        protected={'claim_id','company_id','supplier_id','supplier_commercial_id','approval_id','currency_id','amount','attachment_ids','preview_fingerprint'}
        if any(protected & v.keys() for v in values) or any('default_'+f in self.env.context for f in protected):
            raise AccessError('The claim and approval identity comes only from the protected submission.')
        for vals in values:
            sub=self.env[SUBMISSION].browse(vals.get('submission_id',self.env.context.get('default_submission_id')))
            sub.ensure_one(); sub.check_access('read'); sub._current()
            if sub.state not in ('draft','ready'): raise ValidationError('This revision already has an outgoing attempt.')
        return super().create(values)

    def write(self, values):
        if {'submission_id','claim_id','company_id','supplier_id','supplier_commercial_id','approval_id','currency_id','amount','attachment_ids','preview_fingerprint'} & values.keys():
            raise AccessError('Wizard binding and preview authorization cannot be changed directly.')
        if {'recipient_partner_id','recipient_email','cc_partner_ids','subject','body_html','evidence_ids'} & values.keys():
            values=dict(values,confirmed=False,preview_fingerprint=False)
        return super().write(values)

    def _content(self):
        self.ensure_one(); self.check_access('write')
        return self.submission_id._content(self.recipient_partner_id,self.recipient_email,self.cc_partner_ids,
            self.subject,self.body_html,self.evidence_ids)

    def action_preview(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            sub=self.submission_id
            sub._lock()
            if sub.state not in ('draft','ready'): raise ValidationError('This revision has already been authorized.')
            content,attachments,policy=self._content()
            fingerprint=sub._preview(content,attachments,policy,self.evidence_ids)
            super(SupplierSendWizard,self).write({'preview_fingerprint':fingerprint,'confirmed':False})
        return sub.action_open_pdf()

    def action_preview_email(self):
        self.action_preview()
        return {'type':'ir.actions.act_window','res_model':self._name,'res_id':self.id,'view_mode':'form','target':'new'}

    def action_submit_to_supplier(self):
        self.ensure_one(); self.check_access('write')
        with self.env.cr.savepoint():
            sub=self.submission_id
            sub._lock()
            if not self.confirmed or not self.preview_fingerprint:
                raise ValidationError('Preview the PDF/package and explicitly confirm the outgoing content before sending.')
            if sub.state in ('queued','sent','failed') and sub.content_fingerprint==self.preview_fingerprint and sub.mail_id:
                sub.claim_id._submission_authority(sub.approval_id)
                return sub._form_action()
            content,attachments,policy=self._content()
            from .evidence_verification import digest
            if digest(content)!=self.preview_fingerprint or self.preview_fingerprint!=sub.content_fingerprint:
                raise ValidationError('Recipients, message or selected evidence changed. Preview the exact package again.')
            return sub._queue(self.preview_fingerprint)


class SharingWizard(models.TransientModel):
    _name='supplier.shortage.sharing.wizard'
    _description='Authorize Verified Evidence Disclosure'
    evidence_id=fields.Many2one('supplier.shortage.claim.evidence',required=True)
    approved=fields.Boolean(default=True)
    required=fields.Boolean(string='Mandatory external file')
    reason=fields.Text(required=True)
    def action_apply(self):
        self.ensure_one(); self.check_access('write')
        self.evidence_id.action_set_external_sharing(self.approved,self.reason,self.required)
        return {'type':'ir.actions.act_window_close'}


class SubmissionManagement(models.TransientModel):
    _name='supplier.shortage.submission.manage.wizard'
    _description='Controlled Supplier Communication Action'
    submission_id=fields.Many2one(SUBMISSION,required=True)
    operation=fields.Selection([('response','Record Supplier Response'),('followup','Prepare Follow-up'),
        ('revision','Prepare Corrected Submission'),('retry','Retry Existing Email'),('cancel','Cancel Pending Submission')],default='response',required=True)
    response_kind=fields.Selection([('phone','Phone Call'),('letter','Supplier Letter'),('acknowledgment','Acknowledgment'),
        ('information','More Information Requested'),('disagreement','Supplier Disagreement'),('note','Manual Note')],default='note')
    partner_id=fields.Many2one('res.partner',string='Verified Supplier Contact')
    reason=fields.Text(required=True)
    acknowledge_uncertain=fields.Boolean(string='I checked the existing mail and accept that the previous SMTP outcome may be uncertain')
    def action_apply(self):
        self.ensure_one(); self.check_access('write')
        sub=self.submission_id
        if self.operation=='retry': sub.action_retry(self.reason,self.acknowledge_uncertain)
        elif self.operation=='cancel': sub.action_cancel(self.reason)
        elif self.operation in ('revision','followup'):
            return sub.action_prepare_revision(self.reason,self.operation=='followup')
        else: sub.action_record_response(self.response_kind,self.reason,self.partner_id.id or None)
        return {'type':'ir.actions.act_window_close'}
