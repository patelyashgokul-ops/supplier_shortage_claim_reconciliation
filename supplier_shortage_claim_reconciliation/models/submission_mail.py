"""Protected native queue integration. Capabilities are Python-local, never RPC context flags."""
from contextvars import ContextVar
import io
from datetime import datetime
from dateutil.relativedelta import relativedelta
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.addons.base.models.ir_mail_server import MailDeliveryException
from .claim_submission import SUBMISSION
from .evidence_verification import digest

MAIL_CAPABILITY=ContextVar('ssc_native_mail_capability',default=False)
PDF_CAPABILITY=ContextVar('ssc_validated_pdf_render',default=False)
NATIVE_PROCESSING=ContextVar('ssc_native_processing',default=False)
TEMPLATE_RENDERING=ContextVar('ssc_content_only_template_render',default=False)


class SubmissionDispatchAttempt(models.Model):
    _name='supplier.shortage.dispatch.attempt'
    _description='Durable Supplier Mail Dispatch Guard'
    _log_access=False
    request_key=fields.Char(required=True,index=True)
    retry_no=fields.Integer(required=True)
    mail_key=fields.Integer(required=True,index=True)
    company_key=fields.Integer(required=True,index=True)
    claim_key=fields.Integer(required=True)
    authorizer_key=fields.Integer(required=True)
    reserved_at=fields.Datetime(required=True)
    _attempt_unique=models.Constraint('UNIQUE(request_key,retry_no)','This authorized dispatch attempt has already been reserved.')

    @api.model_create_multi
    def create(self, values): raise AccessError('Dispatch guards are reserved only by validated native queue processing.')
    def write(self, values): raise AccessError('Dispatch reservations are immutable.')
    def unlink(self): raise AccessError('Dispatch reservation history cannot be deleted.')
    def copy(self, default=None): raise AccessError('Dispatch reservations cannot be copied.')

    def _reserve(self, submission, mail):
        submission.claim_id._submission_authority(submission.approval_id)
        if submission.mail_id.id!=mail.id:
            raise ValidationError('Dispatch reservation must reference the protected submission email.')
        # Transport-only durability boundary, never called by business queue creation.
        # SMTP cannot participate in the business transaction. Commit only this
        # minimal technical marker before entering native SMTP, so a crash cannot
        # cause an automatic resend. Integer references deliberately avoid locks
        # on business rows held by the native processor's transaction. No mail,
        # claim, source, attachment, approval or business audit is committed here.
        with self.env.registry.cursor() as cr:
            cr.execute('''INSERT INTO supplier_shortage_dispatch_attempt
                (request_key,retry_no,mail_key,company_key,claim_key,authorizer_key,reserved_at)
                VALUES (%s,%s,%s,%s,%s,%s,statement_timestamp() AT TIME ZONE 'UTC')
                ON CONFLICT (request_key,retry_no) DO NOTHING RETURNING id''',
                [submission.idempotency_key,submission.retry_count,mail.id,submission.company_id.id,
                 submission.claim_id.id,submission.dispatch_authorized_by.id or submission.authorized_by.id])
            reserved=bool(cr.fetchone())
            cr.commit()
        return reserved


class SubmissionAttachments(models.Model):
    _inherit='ir.attachment'

    @api.model_create_multi
    def create(self, values):
        if any(v.get('res_model',self.env.context.get('default_res_model'))==SUBMISSION for v in values):
            raise AccessError('Submission files are generated only from validated package selection.')
        return super().create(values)

    def _create_submission_file(self, submission, values):
        submission.ensure_one(); submission.check_access('write')
        submission.claim_id._submission_authority(submission.approval_id)
        if submission.state not in ('draft','ready'):
            raise AccessError('Files of an authorized submission cannot be changed.')
        from .ir_attachment import IrAttachment
        values=dict(values,res_model=SUBMISSION,res_id=submission.id,company_id=submission.company_id.id,
            type='binary',public=False,access_token=False,res_field=False)
        return super(IrAttachment,self.with_context({})).create(values)

    def write(self, values):
        if any(a.res_model==SUBMISSION for a in self) or values.get('res_model')==SUBMISSION:
            raise AccessError('Frozen supplier package files and associations are immutable.')
        return super().write(values)

    def unlink(self):
        if any(a.res_model==SUBMISSION for a in self): raise AccessError('Supplier package files are retained for history.')
        return super().unlink()

    def copy(self, default=None):
        if any(a.res_model==SUBMISSION for a in self): raise AccessError('Use an authorized new submission revision.')
        return super().copy(default)

    def _claim_document(self, model, record_id, operation):
        if model==SUBMISSION and record_id:
            sub=self.env[model].browse(record_id)
            sub.check_access(operation)
            return sub.claim_id
        return super()._claim_document(model,record_id,operation)

    def generate_access_token(self):
        if any(a.res_model==SUBMISSION for a in self): raise AccessError('Supplier package download tokens are not provided.')
        return super().generate_access_token()


class SubmissionMail(models.Model):
    _inherit='mail.mail'

    def _managed(self):
        return self.filtered(lambda m:m.model==SUBMISSION and m.res_id)

    @api.model_create_multi
    def create(self, values):
        if not MAIL_CAPABILITY.get():
            for v in values:
                model=v.get('model',self.env.context.get('default_model'))
                message=self.env['mail.message'].browse(v.get('mail_message_id')) if v.get('mail_message_id') else self.env['mail.message']
                if model==SUBMISSION or (message and message.model==SUBMISSION):
                    raise AccessError('Supplier claim email must be explicitly authorized through its exact package preview.')
        return super().create(values)

    def write(self, values):
        managed=self._managed()
        if not MAIL_CAPABILITY.get() and (managed or values.get('model')==SUBMISSION):
            raise AccessError('Use supplier submission tracking, cancellation or controlled retry.')
        if managed and values.get('failure_reason'):
            values=dict(values,failure_reason='Outgoing processing failed. Review the failure category and protected administrator server logs; SMTP outcome may be uncertain.')
        result=super().write(values)
        # Odoo writes a provisional 'exception' before SMTP to acquire locks.
        # It is not a real failure: synchronize that state only at postprocessing.
        if managed and 'state' in values and not (NATIVE_PROCESSING.get() and values['state']=='exception'):
            managed._sync_submission_status()
        return result

    def _postprocess_sent_message(self, success_pids, success_emails, failure_reason=False, failure_type=None):
        result=super()._postprocess_sent_message(success_pids,success_emails,failure_reason=failure_reason,failure_type=failure_type)
        self._managed()._sync_submission_status()
        return result

    def unlink(self):
        if self._managed(): raise AccessError('Submission emails and native message references are retained.')
        return super().unlink()

    def copy(self, default=None):
        if self._managed(): raise AccessError('Submission emails cannot be copied or resent implicitly.')
        return super().copy(default)

    @api.autovacuum
    def _gc_canceled_mail_mail(self):
        # Preserve managed history without preventing native cleanup of other mail.
        months=self.env['ir.config_parameter'].sudo().get_int('mass_mailing.cancelled_mails_months_limit',6)
        if months<=0: return
        old=self.with_context(active_test=False).search([('state','=','cancel'),
            ('write_date','<=',datetime.utcnow()-relativedelta(months=months)),('model','!=',SUBMISSION)],order='id asc',limit=10000)
        old.with_context(prefetch_fields=False).unlink()

    def _sync_submission_status(self):
        for mail in self._managed():
            sub=self.env[SUBMISSION].browse(mail.res_id)
            if sub.mail_id.id!=mail.id: continue
            state={'sent':'sent','exception':'failed','cancel':'canceled'}.get(mail.state)
            if not state or sub.state==state: continue
            # Do not expose native SMTP exception text (which can contain credentials).
            values={'state':state,'last_failure':'Outgoing processing failed; review the protected native email/server log and confirm any uncertain SMTP outcome before retry.' if state=='failed' else False}
            if state=='sent': values['sent_at']=fields.Datetime.now()
            if state=='sent' and sub.purpose=='followup': values['active_cycle_key']=False
            sub._set(values)
            sub._audit('Native outgoing processing status: %s. Recipient delivery and acceptance remain unverified.'%state)

    def send(self, auto_commit=False, raise_exception=False, post_send_callback=None):
        # Native scheduler and manual native sends both pass this fresh validation.
        managed=self._managed()
        ordinary=self-managed
        if ordinary:
            super(SubmissionMail,ordinary).send(auto_commit=auto_commit,raise_exception=raise_exception,post_send_callback=post_send_callback)
        for mail in managed:
            mail.lock_for_update(); mail.invalidate_recordset()
            if mail.state!='outgoing': raise ValidationError('Only a queued supplier email may be processed. Use controlled retry for failures.')
            sub=self.env[SUBMISSION].browse(mail.res_id)
            if not self.env.su:
                sub.claim_id._submission_authority(sub.approval_id)
            actor=sub.dispatch_authorized_by or sub.authorized_by
            try:
                if not actor or not actor.active: raise AccessError('The submission authorizer is unavailable; processing is blocked.')
                actual=sub.with_user(actor).with_context({},allowed_company_ids=sub.company_id.ids)
                if actual.mail_id.id!=mail.id or actual.state!='queued':
                    raise ValidationError('This native email is not the authorized current queued attempt.')
                actual._lock(); policy=actual._current()
                actual._package(actual.selected_evidence_ids,policy)
                actual._check_size(actual.attachment_ids,actual.body_html,policy)
                to,cc=actual._recipient(actual.recipient_partner_id,actual.recipient_email,actual.cc_partner_ids)
                if cc!=(actual.cc_emails or '') or actual.package_checksum!=digest([(a.id,a.checksum,a.file_size) for a in actual.attachment_ids.sorted('id')]):
                    raise ValidationError('The approved recipient/package integrity changed.')
                if set(mail.attachment_ids.ids)!=set(actual.attachment_ids.ids) or mail.email_to!=actual.recipient_email or (mail.email_cc or '')!=(actual.cc_emails or '') or mail.subject!=actual.subject or str(mail.body_html)!=str(actual.body_html) or mail.email_from!=actual.sender_email or mail.reply_to!=actual.reply_to or mail.recipient_ids or mail.recipient_cc_ids:
                    raise ValidationError('The native email differs from its frozen authorized envelope.')
            except (AccessError,ValidationError,UserError):
                token=MAIL_CAPABILITY.set(True)
                try: mail.sudo().write({'state':'exception','failure_type':'unknown','failure_reason':'Claim approval, recipient, consent or frozen package is no longer eligible. Obtain authorized review.'})
                finally: MAIL_CAPABILITY.reset(token)
                if raise_exception: raise
                continue
            if not self.env['supplier.shortage.dispatch.attempt']._reserve(actual,mail):
                token=MAIL_CAPABILITY.set(True)
                try: mail.sudo().write({'state':'exception','failure_type':'unknown','failure_reason':'A previous dispatch attempt exists; its SMTP outcome may be uncertain. Explicit reviewed retry is required.'})
                finally: MAIL_CAPABILITY.reset(token)
                sub._set({'last_failure':'A prior dispatch attempt already started. Delivery is uncertain; check the existing message with an administrator and explicitly authorize any retry.'})
                sub._audit('Automatic redispatch blocked by durable attempt guard; SMTP outcome requires review.')
                continue
            # Process one managed email at a time. Native scheduled commits must
            # not release another claim's locks before its fresh validation.
            token=MAIL_CAPABILITY.set(True)
            processing=NATIVE_PROCESSING.set(True)
            try:
                super(SubmissionMail,mail.with_context({})).send(auto_commit=auto_commit,raise_exception=raise_exception,post_send_callback=post_send_callback)
            except MailDeliveryException:
                raise UserError('Outgoing transport failed. Review protected server logs before any retry; delivery remains unverified.') from None
            finally:
                NATIVE_PROCESSING.reset(processing)
                MAIL_CAPABILITY.reset(token)
        return True


class SubmissionMessage(models.Model):
    _inherit='mail.message'

    @api.model_create_multi
    def create(self, values):
        if not MAIL_CAPABILITY.get() and any(v.get('model',self.env.context.get('default_model'))==SUBMISSION for v in values):
            raise AccessError('Submission messages are created only by explicit queue authorization.')
        return super().create(values)

    def write(self, values):
        if not MAIL_CAPABILITY.get() and (any(m.model==SUBMISSION for m in self) or values.get('model')==SUBMISSION):
            raise AccessError('Submitted native message content is protected.')
        return super().write(values)

    def unlink(self):
        if any(m.model==SUBMISSION for m in self): raise AccessError('Submitted native message history is retained.')
        return super().unlink()

    def copy(self, default=None):
        if any(m.model==SUBMISSION for m in self): raise AccessError('Submitted messages cannot be copied.')
        return super().copy(default)


class SubmissionReport(models.AbstractModel):
    _name='report.supplier_shortage_claim_reconciliation.report_claim_statement_document'
    _table='ssc_submission_statement_report'
    _description='Authorized Frozen Supplier Claim Statement'

    def _get_report_values(self, docids, data=None):
        docs=self.env[SUBMISSION].browse(docids)
        docs.check_access('read')
        if not PDF_CAPABILITY.get(): raise AccessError('Use the preserved PDF download or the validated package preview.')
        return {'docs':docs,'doc_ids':docs.ids,'doc_model':SUBMISSION}


class SupplierReportAction(models.Model):
    _inherit='ir.actions.report'

    def _get_layout(self):
        if PDF_CAPABILITY.get():
            return self.env.ref('supplier_shortage_claim_reconciliation.report_frozen_minimal_layout')
        return super()._get_layout()

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        report=self._get_report(report_ref)
        if report.report_name=='supplier_shortage_claim_reconciliation.report_claim_statement_document' and not PDF_CAPABILITY.get():
            subs=self.env[SUBMISSION].browse([res_ids] if isinstance(res_ids,int) else res_ids or [])
            subs.check_access('read')
            if not subs or any(not s.pdf_attachment_id for s in subs):
                raise ValidationError('Use the authorized Send Wizard to preview the PDF first.')
            subs.pdf_attachment_id.check_access('read')
            streams=[s.pdf_attachment_id.raw.content for s in subs]
            return (streams[0] if len(streams)==1 else self._merge_pdfs([io.BytesIO(b) for b in streams]).getvalue()),'pdf'
        return super()._render_qweb_pdf(report_ref,res_ids=res_ids,data=data)
