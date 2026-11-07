"""Explicit supplier communication. Submission never records acceptance or recovery."""
import hashlib
import math
import re
import uuid
from odoo import api, fields, models, Command, tools
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.misc import format_amount
from odoo.tools.image import image_data_uri
from odoo.addons.mail.tools.attachment import extract_attachment_ids_from_html
from .claim_preparation import ROLE, lock_records
from .claim_approval import ApprovalPolicy
from .evidence_verification import digest

SUBMISSION = 'supplier.shortage.claim.submission'
EXTERNAL_FIELDS = {'external_share_approved','external_share_required','external_share_by','external_share_at','external_share_reason'}


def email_address(value):
    if not isinstance(value,str) or '\r' in value or '\n' in value:
        raise ValidationError('Select a single valid email address.')
    result=tools.email_normalize(value,strict=True)
    if not result:
        raise ValidationError('The selected contact needs a valid email address.')
    return result


def require_config(env):
    if env.su or not env.user.has_group(ROLE+'group_module_admin'):
        raise AccessError('Submission configuration requires an authenticated Module Administrator.')


class SubmissionPolicy(models.Model):
    _name='supplier.shortage.submission.policy'
    _description='Company Supplier Submission Policy'
    _check_company_auto=True
    name=fields.Char(required=True)
    company_id=fields.Many2one('res.company',required=True,default=lambda s:s.env.company,index=True)
    active=fields.Boolean(default=True)
    template_id=fields.Many2one('mail.template',required=True,ondelete='restrict',
        default=lambda s:s.env.ref(ROLE+'mail_template_supplier_claim',raise_if_not_found=False))
    max_email_bytes=fields.Integer(default=10*1024*1024,required=True,help='MIME size estimate including encoding overhead.')
    max_files=fields.Integer(default=25,required=True,help='Maximum file count including the primary claim PDF.')
    require_supporting_file=fields.Boolean(default=True)
    high_value_finance_required=fields.Boolean(default=True)
    configuration_notes=fields.Text()
    _company_unique=models.Constraint('UNIQUE(company_id)','Maintain one submission policy per company.')

    @api.model_create_multi
    def create(self, values):
        require_config(self.env)
        values=[dict(v,company_id=v.get('company_id') or self.env.company.id) for v in values]
        if any(v['company_id'] not in self.env.companies.ids for v in values):
            raise AccessError('Configure only activated authorized companies.')
        return super().create(values)

    def write(self, values):
        require_config(self.env)
        self.check_access('write')
        if 'company_id' in values: raise AccessError('Submission policy company is immutable.')
        return super().write(values)

    def unlink(self):
        raise AccessError('Archive submission policies; retain their history.')

    @api.constrains('template_id','max_email_bytes','max_files')
    def _check_policy(self):
        for p in self:
            if p.template_id.model!=SUBMISSION or p.max_email_bytes<65536 or p.max_files<1:
                raise ValidationError('Use a submission email template and a size limit of at least 64 KiB.')


class SupplierClaimContact(models.Model):
    _name='supplier.shortage.supplier.contact'
    _description='Company Supplier Claim Contact'
    _check_company_auto=True
    company_id=fields.Many2one('res.company',required=True,default=lambda s:s.env.company,index=True)
    supplier_id=fields.Many2one('res.partner',required=True,check_company=True,ondelete='restrict')
    contact_id=fields.Many2one('res.partner',required=True,check_company=True,ondelete='restrict')
    _contact_unique=models.Constraint('UNIQUE(company_id,supplier_id)','This company already has a preferred claim contact for this supplier.')

    @api.model_create_multi
    def create(self, values):
        require_config(self.env)
        if any(v.get('company_id',self.env.company.id) not in self.env.companies.ids for v in values):
            raise AccessError('Configure only an activated authorized company.')
        return super().create(values)

    def write(self, values):
        require_config(self.env)
        if {'company_id','supplier_id'} & values.keys(): raise AccessError('Contact mapping identity is immutable.')
        return super().write(values)

    @api.constrains('company_id','supplier_id','contact_id')
    def _check_contact(self):
        for p in self:
            p.supplier_id.check_access('read'); p.contact_id.check_access('read')
            if p.contact_id.commercial_partner_id!=p.supplier_id.commercial_partner_id:
                raise ValidationError('The preferred contact must belong to this supplier.')
            email_address(p.contact_id.email or '')


class SubmissionClaim(models.Model):
    _inherit='supplier.shortage.claim'
    submission_ids=fields.One2many(SUBMISSION,'claim_id',readonly=True,copy=False)
    communication_ids=fields.One2many('supplier.shortage.claim.communication','claim_id',readonly=True,copy=False)

    def _approval_lock(self):
        from .submission_mail import TEMPLATE_RENDERING
        if TEMPLATE_RENDERING.get(): raise AccessError('Email template rendering cannot perform claim workflow actions.')
        return super()._approval_lock()

    @api.model_create_multi
    def create(self, values):
        protected={'submission_ids','communication_ids'}
        if any(protected & v.keys() for v in values) or any('default_'+f in self.env.context for f in protected):
            raise AccessError('Supplier communications are created by controlled business actions.')
        return super().create(values)

    def write(self, values):
        if {'submission_ids','communication_ids'} & values.keys():
            raise AccessError('Supplier communications cannot be assigned directly.')
        return super().write(values)

    def _submission_authority(self, cycle=None, administrative=False):
        from .submission_mail import TEMPLATE_RENDERING
        if TEMPLATE_RENDERING.get(): raise AccessError('Template rendering is content-only; explicit employee actions are required.')
        self.ensure_one()
        self.check_access('write')
        if self.env.su or self.company_id not in self.env.companies or self.company_id not in self.env.user.company_ids:
            raise AccessError('Supplier communication requires an authenticated employee in the activated claim company.')
        if not self.env.user.has_group(ROLE+'group_external_sender'):
            raise AccessError('Explicit Supplier Communication authority is required.')
        if administrative:
            if not any(self.env.user.has_group(ROLE+g) for g in ('group_claim_manager','group_finance_manager')):
                raise AccessError('Stopping communications or recording responses requires a Purchase or Finance manager.')
            return self.env['supplier.shortage.submission.policy']
        cycle=cycle or self.approval_cycle_ids.filtered(lambda c:c.number==self.approval_cycle)
        policies=self.env['supplier.shortage.submission.policy'].search([('company_id','=',self.company_id.id),('active','=',True)])
        if len(policies)!=1: raise ValidationError('Configure an active company supplier submission policy first.')
        group='group_finance_manager' if cycle and cycle.snapshot['policy']['approval_chain']=='finance' and policies.high_value_finance_required else 'group_claim_manager'
        if not self.env.user.has_group(ROLE+group):
            raise AccessError('This company policy requires the appropriate Purchase or Finance submission role.')
        email_address(self.company_id.email or '')
        return policies

    def _submission_eligibility(self):
        self.ensure_one()
        self._approval_lock()
        cycle=self.approval_cycle_ids.filtered(lambda c:c.number==self.approval_cycle)
        policy=self._submission_authority(cycle)
        if self.approval_status!='approved' or len(cycle)!=1 or cycle.state!='approved':
            raise ValidationError('A current final internal approval is required for supplier submission.')
        cycle.check_access('read')
        self._approval_ready()
        if digest(self._approval_basis())!=cycle.fingerprint:
            raise ValidationError('The approved claim basis changed; obtain fresh internal approval.')
        return cycle,policy

    def action_prepare_supplier_submission(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            cycle,policy=self._submission_eligibility()
            key='%s:%s'%(self.id,cycle.id)
            previous=self.env[SUBMISSION].search([('claim_id','=',self.id),('active_cycle_key','=',key)])
            if previous: return previous._form_action()
            if self.env[SUBMISSION].search_count([('claim_id','=',self.id),('state','=','queued')]):
                raise ValidationError('Resolve or explicitly cancel the existing queued submission before preparing another revision.')
            submission=self.env[SUBMISSION]._prepare(self,cycle,policy)
            return submission._form_action()


class ExternalEvidence(models.Model):
    _inherit='supplier.shortage.claim.evidence'
    external_share_approved=fields.Boolean(readonly=True,copy=False)
    external_share_required=fields.Boolean(readonly=True,copy=False)
    external_share_by=fields.Many2one('res.users',readonly=True,copy=False,ondelete='restrict')
    external_share_at=fields.Datetime(readonly=True,copy=False)
    external_share_reason=fields.Text(readonly=True,copy=False)

    @api.model_create_multi
    def create(self, values):
        if any(EXTERNAL_FIELDS & v.keys() for v in values) or any('default_'+f in self.env.context for f in EXTERNAL_FIELDS):
            raise AccessError('External disclosure approval is server controlled.')
        return super().create(values)

    def write(self, values):
        if EXTERNAL_FIELDS & values.keys(): raise AccessError('Use the documented external disclosure action.')
        return super().write(values)

    def action_set_external_sharing(self, approved, reason, required=False):
        self.ensure_one()
        if not isinstance(approved,bool) or not isinstance(required,bool):
            raise ValidationError('Disclosure approval and mandatory-file decisions must be explicit booleans.')
        self.check_access('read')
        claim=self.claim_id
        claim._lock_for_edit()
        claim._submission_authority(administrative=not approved)
        lock_records(self)
        self.invalidate_recordset()
        if len((reason or '').strip())<10:
            raise ValidationError('Document why this file is safe to disclose, or why disclosure is withdrawn.')
        if approved and (not self.is_current or self.effective_state!='verified' or not self.attachment_id or self.evidence_type in ('internal','account_note')):
            raise ValidationError('Only a current verified supporting file may be approved for external disclosure; internal notes cannot be shared.')
        if approved: self.attachment_id.check_access('read')
        # Consent is separate from evidence verification and does not change the approved valuation.
        from .claim_evidence import ClaimEvidence
        super(ClaimEvidence,self).write({'external_share_approved':bool(approved),'external_share_required':bool(required and approved),
            'external_share_by':self.env.uid,'external_share_at':fields.Datetime.now(),'external_share_reason':reason})
        claim._audit('submission_event','External disclosure %s for evidence %s.'%('authorized' if approved else 'withdrawn',self.id),reason=reason,
            basis={claim.id:{'evidence_id':self.id,'revision':self.revision_number,'required':bool(required and approved)}})
        return True

    def action_open_external_sharing(self):
        self.ensure_one(); self.check_access('read')
        return {'type':'ir.actions.act_window','res_model':'supplier.shortage.sharing.wizard','view_mode':'form','target':'new',
            'context':{'default_evidence_id':self.id}}


class ClaimSubmission(models.Model):
    _name=SUBMISSION
    _description='Supplier Claim Submission Revision'
    _check_company_auto=True
    _order='id desc'
    _rec_name='name'
    _allow_sudo_commands=False
    name=fields.Char(required=True,readonly=True,index=True)
    claim_id=fields.Many2one('supplier.shortage.claim',required=True,check_company=True,ondelete='restrict',index=True)
    company_id=fields.Many2one('res.company',required=True,index=True)
    supplier_id=fields.Many2one('res.partner',required=True,check_company=True,ondelete='restrict')
    approval_id=fields.Many2one('supplier.shortage.approval.cycle',required=True,check_company=True,ondelete='restrict')
    approval_status=fields.Selection(related='claim_id.approval_status')
    revision=fields.Integer(required=True)
    purpose=fields.Selection([('claim','Claim Submission'),('followup','Manual Follow-up')],required=True,default='claim')
    previous_id=fields.Many2one(SUBMISSION,check_company=True,ondelete='restrict')
    revision_reason=fields.Text()
    state=fields.Selection([('draft','Draft'),('ready','Ready'),('queued','Queued'),('sent','Sent by Infrastructure'),
        ('failed','Failed'),('canceled','Canceled')],required=True,default='draft',index=True)
    superseded=fields.Boolean(default=False)
    active_cycle_key=fields.Char(index=True)
    idempotency_key=fields.Char(required=True,index=True)
    requested_by=fields.Many2one('res.users',required=True,ondelete='restrict')
    authorized_by=fields.Many2one('res.users',ondelete='restrict')
    dispatch_authorized_by=fields.Many2one('res.users',ondelete='restrict')
    requested_at=fields.Datetime(required=True)
    queued_at=fields.Datetime()
    sent_at=fields.Datetime()
    recipient_partner_id=fields.Many2one('res.partner',check_company=True,ondelete='restrict')
    recipient_email=fields.Char()
    cc_emails=fields.Char()
    cc_partner_ids=fields.Many2many('res.partner','ssc_submission_cc_rel','submission_id','partner_id',check_company=True)
    sender_email=fields.Char()
    reply_to=fields.Char()
    subject=fields.Char()
    body_html=fields.Html(sanitize=True)
    amount=fields.Monetary(required=True,aggregator=False)
    currency_id=fields.Many2one('res.currency',required=True)
    external_snapshot=fields.Json(required=True)
    evidence_manifest=fields.Json()
    selected_evidence_ids=fields.Many2many('supplier.shortage.claim.evidence','ssc_submission_evidence_rel','submission_id','evidence_id',check_company=True)
    attachment_ids=fields.Many2many('ir.attachment','ssc_submission_attachment_rel','submission_id','attachment_id')
    pdf_attachment_id=fields.Many2one('ir.attachment',ondelete='restrict')
    content_fingerprint=fields.Char()
    package_checksum=fields.Char()
    mail_id=fields.Many2one('mail.mail',ondelete='restrict',copy=False)
    message_id=fields.Many2one('mail.message',ondelete='restrict',copy=False)
    message_reference=fields.Char()
    last_failure=fields.Char()
    retry_count=fields.Integer()
    delivery_status=fields.Selection([('unknown','Unknown / Not Verified')],default='unknown',required=True)
    communication_ids=fields.One2many('supplier.shortage.claim.communication','submission_id',readonly=True)
    _revision_unique=models.Constraint('UNIQUE(claim_id,revision)','This submission revision already exists.')
    _active_unique=models.Constraint('UNIQUE(active_cycle_key)','An active submission already exists for this approval cycle.')
    _request_unique=models.Constraint('UNIQUE(idempotency_key)','This submission request was already created.')
    _mail_unique=models.Constraint('UNIQUE(mail_id)','A native email can belong to only one submission.')

    def _access_domain(self, operation):
        visible_file=Domain('id','access','read') & Domain('attachment_id','access','read')
        return (super()._access_domain(operation) & Domain('approval_id','access','read')
                & ~Domain('selected_evidence_ids','any!',~visible_file))

    @api.model_create_multi
    def create(self, values): raise AccessError('Prepare submission revisions through the approved claim.')
    def write(self, values): raise AccessError('Submission content and delivery history are server controlled.')
    def unlink(self): raise AccessError('Submission history cannot be deleted.')
    def copy(self, default=None): raise AccessError('Use controlled revision or follow-up preparation.')

    def _set(self, values):
        from .submission_mail import TEMPLATE_RENDERING
        if TEMPLATE_RENDERING.get(): raise AccessError('Templates cannot mutate protected submission records.')
        return super(ClaimSubmission,self).write(values)

    def _audit(self, description, reason=None):
        self.ensure_one()
        self.claim_id._audit('submission_event',description,reason=reason,basis={self.claim_id.id:{
            'submission_id':self.id,'reference':self.name,'revision':self.revision,'approval_cycle':self.approval_id.number,
            'recipient':self.recipient_email,'state':self.state,'attachments':self.attachment_ids.ids,'message_reference':self.message_reference}})

    @api.model
    def _prepare(self, claim, cycle, policy, previous=None, purpose='claim', reason=None):
        from .submission_mail import TEMPLATE_RENDERING
        if TEMPLATE_RENDERING.get(): raise AccessError('Templates cannot prepare or send supplier communications.')
        # Caller holds the claim lock and has validated current approval and authority.
        last=self.search([('claim_id','=',claim.id)],order='revision desc',limit=1)
        previous=previous or last
        revision=(last.revision if last else 0)+1
        mapping=self.env['supplier.shortage.supplier.contact'].search([('company_id','=',claim.company_id.id),
            ('supplier_id','=',claim.partner_id.commercial_partner_id.id)],limit=1)
        supplier=claim.partner_id; company=claim.company_id
        reuse_contact=bool(previous and previous.supplier_id.commercial_partner_id==supplier.commercial_partner_id)
        company.partner_id.check_access('read'); supplier.check_access('read')
        basis=cycle.snapshot['basis']; lines=[]
        for row in basis['financial_lines']:
            line=claim.claim_line_ids.filtered(lambda l:l.id==row['line_id'])
            lines.append(dict(row,product=line.product_id.display_name,code=line.product_id.default_code or '',
                uom=line.purchase_line_id.uom_id.display_name,ordered=line.detection_id.ordered_qty,
                received=line.detection_id.net_received_qty,po_line=str(line.purchase_line_id.sequence),
                unit_amount=row['amount']/row['quantity'] if row['quantity'] else 0))
        snapshot={'company':company.name,'company_address':company.partner_id.contact_address or '',
            'company_email':email_address(company.email or ''),'company_phone':company.phone or '',
            'logo':image_data_uri(company.logo.content) if company.logo else '',
            'supplier':supplier.display_name,'supplier_address':supplier.contact_address or '',
            'claim':claim.name,'claim_date':str(claim.claim_date),'purchase':claim.purchase_id.name,
            'reason':cycle.snapshot.get('commercial_context',{}).get('reason') or claim.shortage_reason,
            'lines':lines,'amount':cycle.amount,'currency':cycle.currency_id.name,
            'currency_symbol':cycle.currency_id.symbol,'currency_position':cycle.currency_id.position,
            'line_count':len(lines),'evidence':[]}
        if cycle.currency_id.compare_amounts(sum(r['amount'] for r in lines),cycle.amount):
            raise ValidationError('Approved line amounts do not reconcile with the approved total.')
        values={'name':'%s / R%03d'%(claim.name,revision),'claim_id':claim.id,'company_id':company.id,'supplier_id':supplier.id,
            'approval_id':cycle.id,'revision':revision,'purpose':purpose,'previous_id':previous.id if previous else False,'revision_reason':reason,
            'active_cycle_key':'%s:%s'%(claim.id,cycle.id) if purpose=='claim' else 'F:%s:%s'%(claim.id,previous.id),'idempotency_key':str(uuid.uuid4()),
            'requested_by':self.env.uid,'requested_at':fields.Datetime.now(),'currency_id':cycle.currency_id.id,'amount':cycle.amount,
            'external_snapshot':snapshot,'recipient_partner_id':(previous.recipient_partner_id if reuse_contact else mapping.contact_id).id,
            'recipient_email':previous.recipient_email if reuse_contact else (mapping.contact_id.email or ''),
            'sender_email':snapshot['company_email'],'reply_to':snapshot['company_email']}
        record=super(ClaimSubmission,self.with_context({})).create(values)
        template=policy.template_id.with_context(lang=(record.recipient_partner_id.lang or self.env.lang))
        template.check_access('read')
        rendering=TEMPLATE_RENDERING.set(True)
        try:
            subject=template._render_field('subject',record.ids,compute_lang=True)[record.id]
            body=template._render_field('body_html',record.ids,compute_lang=True)[record.id]
        finally: TEMPLATE_RENDERING.reset(rendering)
        if purpose=='followup': subject='Follow-up: '+subject
        record._set({'subject':subject,'body_html':body})
        record._audit('Supplier %s revision prepared; no email queued.'%purpose,reason)
        return record

    def _form_action(self):
        self.ensure_one(); self.check_access('read')
        return {'type':'ir.actions.act_window','res_model':SUBMISSION,'res_id':self.id,'view_mode':'form','target':'current'}

    def _money(self, value):
        self.ensure_one()
        return format_amount(self.env,value,self.currency_id)

    def _lock(self):
        self.ensure_one(); self.check_access('read')
        self.claim_id._approval_lock()
        self.lock_for_update(); self.invalidate_recordset()

    def _current(self):
        self.ensure_one()
        cycle,policy=self.claim_id._submission_eligibility()
        if cycle!=self.approval_id or cycle.fingerprint!=self.approval_id.fingerprint:
            raise ValidationError('This revision belongs to an old approval. Prepare a newly approved revision.')
        if self.superseded or self.state=='canceled': raise ValidationError('This submission is canceled or superseded.')
        if email_address(self.company_id.email or '')!=self.external_snapshot['company_email']:
            raise ValidationError('The company sender identity changed. Cancel this pending package and prepare a corrected revision.')
        if self.purpose=='followup' and (not self.previous_id or self.previous_id.state!='sent' or self.previous_id.superseded):
            raise ValidationError('Follow-ups require a valid previously sent submission.')
        return policy

    def _recipient(self, contact, address, cc):
        if not contact: raise ValidationError('Explicitly select the correct supplier recipient before preview or submission.')
        contact.ensure_one(); contact.check_access('read'); cc.check_access('read')
        for partner in contact|cc:
            if not partner.active or partner.commercial_partner_id!=self.supplier_id.commercial_partner_id or (partner.company_id and partner.company_id!=self.company_id):
                raise ValidationError('To and CC contacts must belong to this supplier and be permitted in the claim company.')
        normalized=email_address(address or '')
        if normalized!=email_address(contact.email or ''):
            raise ValidationError('The address must match the explicitly selected supplier contact. Correct the contact record first.')
        recipients=[email_address(p.email or '') for p in cc.sorted('id')]
        if normalized in recipients or len(set(recipients))!=len(recipients): raise ValidationError('Remove duplicate To/CC recipients.')
        return normalized,', '.join(recipients)

    def _package(self, evidence, policy):
        evidence.check_access('read')
        lock_records(evidence)
        evidence.invalidate_recordset()
        required=self.claim_id.evidence_ids.filtered(lambda e:e.is_current and e.external_share_required)
        if required-evidence: raise ValidationError('Select every evidence file marked mandatory for external submission.')
        if policy.require_supporting_file and not evidence: raise ValidationError('This company requires at least one explicitly approved supporting file.')
        manifest=[]; seen=set(); attachments=self.env['ir.attachment']
        for e in evidence.sorted('id'):
            if e.claim_id!=self.claim_id or e.company_id!=self.company_id or not e.is_current or e.effective_state!='verified' or not e.external_share_approved or not e.attachment_id or e.evidence_type in ('internal','account_note'):
                raise ValidationError('Select only relevant current verified files explicitly approved for external sharing.')
            a=e.attachment_id
            a.check_access('read')
            if a.company_id!=self.company_id or a.type!='binary' or a.public or a.access_token or not a.file_size:
                raise ValidationError('Supporting files must be private nonempty binary documents in the claim company.')
            manifest.append({'id':e.id,'revision':e.revision_number,'fingerprint':e.reviewed_fingerprint,
                'name':a.name,'description':e.description,'type':dict(e._fields['evidence_type']._description_selection(self.env))[e.evidence_type],
                'attachment_id':a.id,'checksum':a.checksum,'size':a.file_size,'required':e.external_share_required})
            if a.checksum not in seen: seen.add(a.checksum); attachments|=a
        return manifest,attachments

    def _content(self, contact, address, cc, subject, body, evidence):
        cc=cc.sorted('id')
        policy=self._current()
        address,cc_emails=self._recipient(contact,address,cc)
        if not isinstance(subject,str) or '\r' in subject or '\n' in subject or self.claim_id.name not in subject or len(subject)>250:
            raise ValidationError('Use a subject of at most 250 characters containing the unique claim reference and no header line breaks.')
        body=tools.html_sanitize(str(body or ''))
        if len(tools.html2plaintext(body).strip())<20: raise ValidationError('Write a substantive supplier message before previewing.')
        if extract_attachment_ids_from_html(body) or re.search(r'/web/(?:content|image)|/mail/attachment|access_token\s*=',body,re.I):
            raise ValidationError('Private internal document URLs or access-token links are not allowed in supplier email. Select actual approved file attachments.')
        manifest,attachments=self._package(evidence,policy)
        content={'approval':self.approval_id.id,'basis':self.approval_id.fingerprint,'recipient':contact.id,'to':address,'cc':cc_emails,'cc_partners':cc.ids,
            'subject':subject,'body':body,'sender':email_address(self.company_id.email or ''),'manifest':manifest}
        self._check_size(attachments,body,policy)
        return content,attachments,policy

    def _check_size(self, attachments, body, policy):
        headers={'To':self.recipient_email,'Cc':self.cc_emails,'From':self.sender_email,'Reply-To':self.reply_to,'Subject':self.subject}
        estimate=math.ceil(self.env['mail.mail']._estimate_email_size(headers,str(body),attachments.mapped('file_size')))+65536
        native_limit=self.env['ir.mail_server']._get_max_email_size()*1024*1024
        if len(attachments)>policy.max_files or not math.isfinite(native_limit) or native_limit<=0 or estimate>min(policy.max_email_bytes,native_limit):
            raise ValidationError('The selected package exceeds the company MIME email-size limit. Review file names/sizes and reduce the selection; mandatory files may not be omitted.')

    def _preview(self, content, attachments, policy, evidence):
        fingerprint=digest(content)
        if self.state=='ready' and self.content_fingerprint==fingerprint and self.pdf_attachment_id:
            return fingerprint
        snapshot=dict(self.external_snapshot,evidence=content['manifest'],submission_date=str(fields.Date.today()))
        self._set({'external_snapshot':snapshot,'recipient_partner_id':content['recipient'],'recipient_email':content['to'],'cc_emails':content['cc'],
            'cc_partner_ids':[Command.set(content['cc_partners'])],
            'sender_email':content['sender'],'reply_to':content['sender'],'subject':content['subject'],'body_html':content['body']})
        report=self.env.ref(ROLE+'report_supplier_claim_statement')
        from .submission_mail import PDF_CAPABILITY
        token=PDF_CAPABILITY.set(True)
        try:
            pdf,kind=report.with_context(force_report_rendering=True,report_pdf_no_attachment=True)._render_qweb_pdf(report.id,res_ids=self.ids)
        finally: PDF_CAPABILITY.reset(token)
        if kind!='pdf' or not pdf.startswith(b'%PDF-'): raise ValidationError('The PDF renderer did not produce an actual PDF. Check wkhtmltopdf configuration.')
        files=self.env['ir.attachment']
        pdf_attachment=files._create_submission_file(self,{'name':re.sub(r'[\\/:*?"<>|]','_',self.name)+'.pdf','raw':pdf,'mimetype':'application/pdf'})
        files|=pdf_attachment
        for a in attachments:
            files|=self.env['ir.attachment']._create_submission_file(self,{'name':a.name,'raw':a.raw.content,'mimetype':a.mimetype})
        self._check_size(files,content['body'],policy)
        self._set({'state':'ready','evidence_manifest':content['manifest'],'selected_evidence_ids':[Command.set(evidence.ids)],
            'attachment_ids':[Command.set(files.ids)],'pdf_attachment_id':pdf_attachment.id,'content_fingerprint':fingerprint,
            'package_checksum':digest([(a.id,a.checksum,a.file_size) for a in files.sorted('id')])})
        self._audit('Exact PDF and selected evidence package generated for explicit review; no email queued.')
        return fingerprint

    def action_open_composer(self):
        self.ensure_one()
        self._lock(); self._current()
        if self.state not in ('draft','ready'): raise ValidationError('This revision already has an outgoing attempt. Use its tracking, retry or revision actions.')
        wizard=self.env['supplier.shortage.claim.send.wizard'].create({'submission_id':self.id,'recipient_partner_id':self.recipient_partner_id.id,
            'recipient_email':self.recipient_email,'subject':self.subject,'body_html':self.body_html,
            'evidence_ids':[Command.set(self.selected_evidence_ids.ids)]})
        return {'type':'ir.actions.act_window','res_model':wizard._name,'res_id':wizard.id,'view_mode':'form','target':'new'}

    def action_open_pdf(self):
        self.ensure_one(); self.check_access('read')
        if not self.pdf_attachment_id: raise ValidationError('Preview and validate the package first.')
        self.pdf_attachment_id.check_access('read')
        return {'type':'ir.actions.act_url','url':'/web/content/%s?download=true'%self.pdf_attachment_id.id,'target':'new'}

    def _queue(self, fingerprint):
        from .submission_mail import MAIL_CAPABILITY
        self._lock()
        if self.state in ('queued','sent','failed'):
            if self.content_fingerprint==fingerprint and self.mail_id: return self._form_action()
            raise ValidationError('An outgoing attempt already exists for this revision.')
        policy=self._current()
        if self.state!='ready' or fingerprint!=self.content_fingerprint: raise ValidationError('Preview and confirm the exact current package first.')
        # Source consent, recipient, approved values and package bytes are all rechecked.
        self._package(self.selected_evidence_ids,policy)
        to,cc=self._recipient(self.recipient_partner_id,self.recipient_email,self.cc_partner_ids)
        if cc!=(self.cc_emails or ''): raise ValidationError('A CC contact changed. Prepare and preview the corrected recipient list.')
        if self.package_checksum!=digest([(a.id,a.checksum,a.file_size) for a in self.attachment_ids.sorted('id')]):
            raise ValidationError('Frozen package integrity failed.')
        self._check_size(self.attachment_ids,self.body_html,policy)
        if self.sender_email!=email_address(self.company_id.email or ''): raise ValidationError('The company sender changed. Preview a corrected revision.')
        token=MAIL_CAPABILITY.set(True)
        try:
            # Narrow queue insertion only; authorization and all document reads used the real employee.
            mail=self.env['mail.mail'].sudo().with_context({}).create({'model':self._name,'res_id':self.id,
                'subject':self.subject,'body_html':str(self.body_html),'email_from':self.sender_email,'reply_to':self.reply_to,
                'email_to':self.recipient_email,'email_cc':self.cc_emails or False,'author_id':self.env.user.partner_id.id,
                'attachment_ids':[Command.set(self.attachment_ids.ids)],'auto_delete':False,'state':'outgoing',
                'message_type':'email_outgoing','recipient_ids':[Command.clear()],'recipient_cc_ids':[Command.clear()]})
        finally: MAIL_CAPABILITY.reset(token)
        self._set({'state':'queued','mail_id':mail.id,'message_id':mail.mail_message_id.id,
            'message_reference':mail.mail_message_id.message_id,'authorized_by':self.env.uid,'dispatch_authorized_by':self.env.uid,'queued_at':fields.Datetime.now(),'last_failure':False})
        if self.purpose=='claim':
            self.search([('claim_id','=',self.claim_id.id),('id','!=',self.id),('purpose','=','claim'),('state','=','sent')])._set({'superseded':True})
        self.env['supplier.shortage.claim.communication']._append(self,'outbound','email','Supplier email explicitly authorized and queued.',self.message_id)
        self._audit('Supplier submission queued in native Odoo mail. Delivery remains unknown.')
        return self._form_action()

    def action_cancel(self, reason=None):
        self._lock(); self.claim_id._submission_authority(self.approval_id,administrative=True)
        if self.state not in ('draft','ready','queued','failed') or len((reason or '').strip())<10:
            raise ValidationError('Cancel only a pending/failed submission with a documented reason; sent mail cannot be retracted.')
        if self.mail_id:
            from .submission_mail import MAIL_CAPABILITY
            token=MAIL_CAPABILITY.set(True)
            try: self.mail_id.sudo().write({'state':'cancel'})
            finally: MAIL_CAPABILITY.reset(token)
        self._set({'state':'canceled','active_cycle_key':False})
        self._audit('Pending supplier submission canceled; documents retained.',reason)
        return True

    def action_retry(self, reason=None, acknowledge_uncertain=False):
        self._lock(); self._current()
        # Native outgoing-mail CRUD is administrator-only in this Odoo build.
        # Inspect only the server-bound queue row, after business authorization;
        # never elevate evidence/source reads or grant general mail access.
        mail=self.mail_id.sudo()
        if self.state!='failed' or not mail or mail.state!='exception' or mail.model!=self._name or mail.res_id!=self.id or acknowledge_uncertain is not True or len((reason or '').strip())<10:
            raise ValidationError('Review the failed native email, acknowledge possible uncertain SMTP outcome and document a retry reason.')
        self._package(self.selected_evidence_ids,self._current())
        from .submission_mail import MAIL_CAPABILITY
        self.env.cr.execute('SELECT COALESCE(MAX(retry_no),-1) FROM supplier_shortage_dispatch_attempt WHERE request_key=%s',[self.idempotency_key])
        next_retry=max(self.retry_count+1,self.env.cr.fetchone()[0]+1)
        token=MAIL_CAPABILITY.set(True)
        try: mail.write({'state':'outgoing','failure_reason':False,'failure_type':False})
        finally: MAIL_CAPABILITY.reset(token)
        self._set({'state':'queued','retry_count':next_retry,'dispatch_authorized_by':self.env.uid,'last_failure':False})
        self._audit('Explicit safe retry queued using the same native email and frozen revision.',reason)
        return True

    def action_prepare_revision(self, reason=None, followup=False):
        self._lock(); policy=self._current()
        if self.state!='sent' or len((reason or '').strip())<10:
            raise ValidationError('Prepare a revision/follow-up only from a sent valid submission with a reason. Resolve uncertain or queued attempts first.')
        key='F:%s:%s'%(self.claim_id.id,self.id) if followup else '%s:%s'%(self.claim_id.id,self.approval_id.id)
        existing=self.search([('claim_id','=',self.claim_id.id),('active_cycle_key','=',key),('id','!=',self.id)])
        if existing: return existing._form_action()
        if not followup: self._set({'active_cycle_key':False})
        new=self._prepare(self.claim_id,self.approval_id,policy,self,'followup' if followup else 'claim',reason)
        return new._form_action()

    def action_open_management(self):
        self.ensure_one(); self.check_access('read')
        return {'type':'ir.actions.act_window','res_model':'supplier.shortage.submission.manage.wizard','view_mode':'form','target':'new',
            'context':{'default_submission_id':self.id}}

    def action_record_response(self, kind, note, partner_id=None):
        self._lock(); self.claim_id._submission_authority(self.approval_id,administrative=True)
        if self.state!='sent' or kind not in ('phone','letter','acknowledgment','information','disagreement','note') or len((note or '').strip())<10:
            raise ValidationError('Record a substantive manually verified response against a sent submission.')
        contact=self.env['res.partner'].browse(partner_id) if partner_id else self.recipient_partner_id
        contact.ensure_one(); contact.check_access('read')
        if contact.commercial_partner_id!=self.supplier_id.commercial_partner_id or (contact.company_id and contact.company_id!=self.company_id):
            raise ValidationError('Response sender must belong to this supplier and company scope.')
        event=self.env['supplier.shortage.claim.communication']._append(self,'inbound',kind,note)
        super(ClaimCommunication,event).write({'partner_id':contact.id})
        self._audit('Supplier response manually recorded; no acceptance, settlement or financial state inferred.',note)
        return True


class ClaimCommunication(models.Model):
    _name='supplier.shortage.claim.communication'
    _description='Protected Supplier Communication History'
    _check_company_auto=True
    _order='id desc'
    submission_id=fields.Many2one(SUBMISSION,required=True,check_company=True,ondelete='restrict',index=True)
    claim_id=fields.Many2one(related='submission_id.claim_id',store=True,index=True)
    company_id=fields.Many2one(related='submission_id.company_id',store=True,index=True)
    partner_id=fields.Many2one('res.partner',check_company=True,ondelete='restrict')
    direction=fields.Selection([('outbound','Outbound'),('inbound','Inbound / Manually Recorded'),('internal','Internal Note')],required=True)
    kind=fields.Selection([('email','Email'),('phone','Phone Call'),('letter','Supplier Letter'),('acknowledgment','Acknowledgment'),
        ('information','More Information Requested'),('disagreement','Supplier Disagreement'),('note','Manual Note')],required=True)
    subject=fields.Char(required=True)
    mail_message_id=fields.Many2one('mail.message',ondelete='restrict')
    message_reference=fields.Char()
    recorded_by=fields.Many2one('res.users',required=True,ondelete='restrict')
    communication_date=fields.Datetime(required=True)
    note=fields.Text()

    @api.model_create_multi
    def create(self, values): raise AccessError('Use the authorized communication logging workflow.')
    def write(self, values): raise AccessError('Communication history is immutable.')
    def unlink(self): raise AccessError('Communication history cannot be deleted.')
    def copy(self, default=None): raise AccessError('Communication history cannot be copied.')

    @api.model
    def _append(self, submission, direction, kind, note, message=None):
        submission.check_access('read')
        return super(ClaimCommunication,self.with_context({})).create({'submission_id':submission.id,'partner_id':submission.recipient_partner_id.id,'direction':direction,'kind':kind,
            'subject':submission.subject or submission.name,'mail_message_id':message.id if message else False,
            'message_reference':submission.message_reference,'recorded_by':self.env.uid,'communication_date':fields.Datetime.now(),'note':note})


class SubmissionAudit(models.Model):
    _inherit='supplier.shortage.claim.audit'
    event_type=fields.Selection(selection_add=[('submission_event','Supplier Submission / Communication')],ondelete={'submission_event':lambda records:None})
