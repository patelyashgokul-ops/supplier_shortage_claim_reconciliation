import base64
import io
from copy import deepcopy
from unittest.mock import patch, MagicMock
from pathlib import Path
from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tests.common import new_test_user
from odoo.tests import Form
from odoo.tools.pdf import PdfFileReader
from .test_claim_preparation import PreparationCase
from .preparation_fixtures import complete_evidence, verify_document, additional_receipt, independent_checker

M='supplier_shortage_claim_reconciliation.'
SUB='supplier.shortage.claim.submission'


class SupplierSubmissionCase(PreparationCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        native=',purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly'
        cls.sender=new_test_user(cls.env,login='ssc_supplier_sender',groups=M+'group_claim_manager,'+M+'group_external_sender'+native,
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        cls.configurator=new_test_user(cls.env,login='ssc_supplier_config',groups=M+'group_module_admin'+native,
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        for user in cls.reviewer|cls.finance:
            user.write({'group_ids':[Command.link(cls.env.ref('stock.group_stock_manager').id)]})
        cls.finance.write({'group_ids':[Command.link(cls.env.ref(M+'group_external_sender').id)]})
        cls.company.email='claims-company@example.invalid'
        cls.supplier.email='supplier-claims@example.invalid'
        cls.Approval=cls.env['supplier.shortage.approval.policy'].with_user(cls.configurator)
        cls.Approval.search([('company_id','=',cls.company.id),('active','=',True)]).write({'active':False})
        cls.approval_policy=cls.Approval.create({'name':'Submission test approval','company_id':cls.company.id})
        cls.policy=cls.env['supplier.shortage.submission.policy'].with_user(cls.configurator).create({
            'name':'Test company outbound control','company_id':cls.company.id})
        cls.contact=cls.env['supplier.shortage.supplier.contact'].with_user(cls.configurator).create({
            'company_id':cls.company.id,'supplier_id':cls.supplier.id,'contact_id':cls.supplier.id})

    def setUp(self):
        super().setUp()
        # No SMTP connection is permitted in any test. Individual native processing
        # tests replace this with a fake connection and mock send_email explicitly.
        self.smtp_guard=patch.object(type(self.env['ir.mail_server']),'_connect__',side_effect=AssertionError('Real SMTP forbidden in STEP 13 tests'))
        self.smtp_guard.start(); self.addCleanup(self.smtp_guard.stop)

    def _approved(self, chain='purchase', products=None, reason=None, duplicate=False, stop_before=None, internal=False):
        self.approval_policy.write({'approval_chain':chain})
        claim=self._prepare(self._candidates(products=products))
        claim.shortage_reason=reason or 'Verified delivery shortage supported by posted billing and warehouse reconciliation'
        evidence=self._evidence(claim,upload=True)
        evidence.verification_note='Independently checked delivery-specific warehouse evidence'
        verify_document(evidence)
        if duplicate:
            other=self._evidence(claim,upload=True)
            other.verification_note='Independent review of duplicate delivery evidence reference'
            verify_document(other)
        if internal:
            private=self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({'claim_id':claim.id,
                'evidence_type':'internal','description':'Internal management discussion','statement':'Confidential internal reviewer discussion',
                'scope_line_ids':[Command.set(claim.claim_line_ids.ids)],
                'verification_note':'Independently verified as internal-only company discussion'})
            verify_document(private)
        complete_evidence(claim)
        claim.action_submit_for_review()
        claim.with_user(self.reviewer).action_verify_financial_value('Native bill subtotal, discount, UoM and reserved quantities checked')
        claim.action_request_internal_approval()
        roles=['purchase','accounts','finance'][:{'purchase':1,'accounts':2,'finance':3}[chain]]
        actors={'purchase':self.sender,'accounts':self.reviewer,'finance':self.finance}
        for role in roles:
            if role==stop_before: break
            claim.approval_step_ids.filtered(lambda s:s.role==role).with_user(actors[role]).action_approve()
        sender=self.finance if chain=='finance' else self.sender
        evidence.with_user(sender).action_set_external_sharing(True,'Relevant verified delivery evidence cleared for this supplier',required=True)
        if duplicate: other.with_user(sender).action_set_external_sharing(True,'Duplicate file explicitly cleared; package must deduplicate identical bytes')
        return claim,evidence

    def _prepared(self, **kwargs):
        claim,evidence=self._approved(**kwargs)
        sender=self.finance if kwargs.get('chain')=='finance' else self.sender
        action=claim.with_user(sender).action_prepare_supplier_submission()
        sub=self.env[SUB].with_user(sender).browse(action['res_id'])
        wizard_action=sub.action_open_composer()
        wizard=self.env['supplier.shortage.claim.send.wizard'].with_user(sender).browse(wizard_action['res_id'])
        wizard.evidence_ids=[Command.set(evidence.ids)]
        return claim,evidence,sub,wizard

    def _queued(self, **kwargs):
        claim,evidence,sub,wizard=self._prepared(**kwargs)
        wizard.action_preview()
        wizard.confirmed=True
        wizard.action_submit_to_supplier()
        return claim,evidence,sub,wizard

    def _send_mocked(self, sub, fail=False):
        Server=type(self.env['ir.mail_server'])
        fake=MagicMock()
        with patch.object(Server,'_connect__',return_value=fake),patch.object(Server,'_disable_send',return_value=False),\
                patch.object(Server,'send_email',side_effect=RuntimeError('Mock SMTP transport failed') if fail else None,
                             return_value='<mock-supplier-transport@example.invalid>') as transport:
            sub.mail_id.sudo().send(auto_commit=False)
            return transport.call_count


class TestSupplierSubmission(SupplierSubmissionCase):
    def test_actual_pdf_render_and_exact_authorized_queue(self):
        claim,evidence,sub,wizard=self._prepared()
        counts={m:self.env[m].search_count([]) for m in ('account.move','account.move.line','stock.move','stock.quant','account.payment','mail.mail')}
        wizard.action_preview()
        self.assertEqual(sub.state,'ready')
        pdf=sub.pdf_attachment_id.raw.content
        self.assertTrue(pdf.startswith(b'%PDF-'))
        reader=PdfFileReader(io.BytesIO(pdf))
        text=' '.join(page.extract_text() for page in reader.pages)
        normalized=' '.join(text.split())
        for expected in (claim.name,self.supplier.name,self.company.name,'150.00','Supplier Shortage Claim Statement',self.company.currency_id.name):
            self.assertIn(expected,normalized)
        self.assertEqual(len(sub.attachment_ids),2)
        self.assertEqual(sub.state,'ready')
        self.assertEqual(counts['mail.mail'],self.env['mail.mail'].search_count([]))
        with self.assertRaises(ValidationError): wizard.action_submit_to_supplier()
        wizard.confirmed=True
        wizard.action_submit_to_supplier()
        self.assertEqual(sub.state,'queued')
        self.assertEqual(sub.delivery_status,'unknown')
        self.assertEqual(sub.mail_id.state,'outgoing')
        self.assertEqual(set(sub.mail_id.attachment_ids.ids),set(sub.attachment_ids.ids))
        self.assertEqual(sub.mail_id.email_to,self.supplier.email)
        self.assertEqual(sub.mail_id.email_from,self.company.email)
        for model in counts:
            self.assertEqual(self.env[model].search_count([]),counts[model]+(1 if model=='mail.mail' else 0))
        self.assertEqual(claim.approval_status,'approved')
        self.assertEqual(claim.state,'under_review')

    def test_mocked_native_processing_sent_not_delivered(self):
        claim,evidence,sub,wizard=self._queued()
        self.assertGreater(self._send_mocked(sub),0)
        self.assertEqual(sub.state,'sent')
        self.assertEqual(sub.delivery_status,'unknown')
        self.assertTrue(sub.sent_at)
        self.assertEqual(claim.approval_status,'approved')
        self.assertEqual(claim.state,'under_review')
        self.assertFalse(claim.audit_event_ids.filtered(lambda a:a.event_type=='submission_event' and
            a.change_summary.startswith('Native outgoing processing status: failed')))

    def test_missing_approval_and_unauthorized_sender_blocked(self):
        claim=self._prepare(self._candidates())
        with self.assertRaises(ValidationError): claim.with_user(self.sender).action_prepare_supplier_submission()
        claim,evidence=self._approved()
        with self.assertRaises(AccessError): claim.action_prepare_supplier_submission()
        with self.assertRaises(AccessError): claim.with_user(self.configurator).action_prepare_supplier_submission()
        self.assertFalse(claim.submission_ids)

    def test_composer_and_native_views_load_without_sending(self):
        claim,evidence,sub,wizard=self._prepared()
        before=self.env['mail.mail'].search_count([])
        for model in (SUB,'supplier.shortage.claim.send.wizard','supplier.shortage.claim.communication'):
            self.env[model].with_user(self.sender).get_views([(False,'form')])
        with Form(wizard) as form:
            self.assertEqual(form.recipient_email,self.supplier.email)
        self.assertFalse(sub.mail_id)
        wizard.unlink()
        self.assertEqual(self.env['mail.mail'].search_count([]),before)

    def test_pending_rejected_and_stale_approval_cannot_prepare(self):
        claim,evidence=self._approved('finance')
        self.assertEqual(claim.approval_status,'approved')
        with self.assertRaises(AccessError): claim.with_user(self.sender).action_prepare_supplier_submission()
        # A material source change invalidates the final approval; no revision can bypass it.
        additional_receipt(claim.purchase_id,5)
        with self.assertRaises(ValidationError): claim.with_user(self.finance).action_prepare_supplier_submission()
        self.assertFalse(claim.submission_ids)

    def test_missing_internal_evidence_blocks_approval_and_submission(self):
        claim=self._prepare(self._candidates())
        claim.shortage_reason='Internal supporting checks have not yet been completed'
        with self.assertRaises(ValidationError): claim.action_submit_for_review()
        with self.assertRaises(ValidationError): claim.with_user(self.sender).action_prepare_supplier_submission()

    def test_multi_group_without_explicit_sender_permission_denied(self):
        claim,evidence=self._approved()
        self.user.write({'group_ids':[Command.link(self.env.ref(M+'group_finance_manager').id)]})
        with self.assertRaises(AccessError): claim.action_prepare_supplier_submission()
        with self.assertRaises(AccessError): evidence.action_set_external_sharing(True,'Unauthorized group overlap cannot grant external disclosure')

    def test_invalid_recipient_addresses_supplier_and_cc_rejected(self):
        claim,evidence,sub,wizard=self._prepared()
        stranger=self.env['res.partner'].create({'name':'Unrelated supplier','email':'unrelated@example.invalid'})
        for address in ('','broken-address','a@example.invalid\r\nBcc: thief@example.invalid'):
            wizard.recipient_email=address
            with self.assertRaises(ValidationError): wizard.action_preview()
        wizard.write({'recipient_partner_id':stranger.id,'recipient_email':stranger.email})
        with self.assertRaises(ValidationError): wizard.action_preview()
        wizard.write({'recipient_partner_id':self.supplier.id,'recipient_email':self.supplier.email,
            'cc_partner_ids':[Command.set(stranger.ids)]})
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_explicit_supplier_contact_and_cc_snapshot(self):
        claim,evidence,sub,wizard=self._prepared()
        contact=self.env['res.partner'].create({'name':'Claims Department','parent_id':self.supplier.id,'email':'claims-contact@example.invalid'})
        cc=self.env['res.partner'].create({'name':'Supplier Purchase Contact','parent_id':self.supplier.id,'email':'purchase-contact@example.invalid'})
        wizard.write({'recipient_partner_id':contact.id,'recipient_email':contact.email,'cc_partner_ids':[Command.set(cc.ids)]})
        wizard.action_preview(); wizard.confirmed=True; wizard.action_submit_to_supplier()
        self.assertEqual(sub.recipient_email,contact.email)
        self.assertEqual(sub.mail_id.email_cc,cc.email)
        self.assertEqual(sub.cc_partner_ids,cc)

    def test_missing_preferred_contact_still_opens_empty_composer(self):
        self.contact.unlink()
        claim,evidence,sub,wizard=self._prepared()
        self.assertFalse(wizard.recipient_partner_id)
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_no_implicit_file_disclosure_and_required_file_enforced(self):
        claim,evidence,sub,wizard=self._prepared()
        wizard.evidence_ids=[Command.clear()]
        with self.assertRaises(ValidationError): wizard.action_preview()
        evidence.with_user(self.sender).action_set_external_sharing(False,'Withdraw disclosure until privacy concerns are resolved')
        wizard.evidence_ids=[Command.set(evidence.ids)]
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_unrelated_claim_file_cannot_be_added(self):
        claim,evidence,sub,wizard=self._prepared()
        other,foreign=self._approved()
        wizard.evidence_ids=[Command.set((evidence|foreign).ids)]
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_duplicate_file_bytes_are_frozen_only_once(self):
        claim,evidence,sub,wizard=self._prepared(duplicate=True)
        files=claim.evidence_ids.filtered('attachment_id')
        self.assertEqual(len(files),2)
        wizard.evidence_ids=[Command.set(files.ids)]
        wizard.action_preview()
        self.assertEqual(len(sub.evidence_manifest),2)
        self.assertEqual(len(sub.attachment_ids),2)  # One statement + one unique supporting binary.

    def test_oversize_package_and_native_limit_block_without_links(self):
        claim,evidence,sub,wizard=self._prepared()
        self.policy.max_email_bytes=65536
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.pdf_attachment_id)
        self.policy.max_email_bytes=10*1024*1024
        Server=type(self.env['ir.mail_server'])
        with patch.object(Server,'_get_max_email_size',return_value=0.001):
            with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)
        self.assertFalse(sub.attachment_ids)

    def test_private_download_urls_and_header_injection_blocked(self):
        claim,evidence,sub,wizard=self._prepared()
        wizard.subject=claim.name+'\r\nBcc: unrelated@example.invalid'
        with self.assertRaises(ValidationError): wizard.action_preview()
        wizard.subject=sub.subject
        wizard.body_html='<p>Please review <a href="/web/content/%s?access_token=forged">supporting evidence</a>.</p>'%evidence.attachment_id.id
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_changed_preview_requires_new_explicit_confirmation(self):
        claim,evidence,sub,wizard=self._prepared()
        wizard.action_preview(); first=sub.pdf_attachment_id
        wizard.confirmed=True
        wizard.body_html=str(wizard.body_html)+'<p>Please acknowledge the included discrepancy.</p>'
        self.assertFalse(wizard.confirmed)
        self.assertFalse(wizard.preview_fingerprint)
        with self.assertRaises(ValidationError): wizard.action_submit_to_supplier()
        wizard.action_preview(); wizard.confirmed=True; wizard.action_submit_to_supplier()
        self.assertNotEqual(sub.pdf_attachment_id,first)
        self.assertEqual(sub.state,'queued')

    def test_idempotent_prepare_preview_and_repeated_send(self):
        claim,evidence,sub,wizard=self._prepared()
        again=claim.with_user(self.sender).action_prepare_supplier_submission()
        self.assertEqual(again['res_id'],sub.id)
        wizard.action_preview(); first=sub.pdf_attachment_id
        wizard.action_preview()
        self.assertEqual(first,sub.pdf_attachment_id)
        wizard.confirmed=True; wizard.action_submit_to_supplier()
        mail=sub.mail_id; events=sub.claim_id.audit_event_ids
        wizard.action_submit_to_supplier()
        self.assertEqual(mail,sub.mail_id)
        self.assertEqual(events,sub.claim_id.audit_event_ids)
        self.assertEqual(self.env['mail.mail'].search_count([('model','=',SUB),('res_id','=',sub.id)]),1)

    def test_native_failure_masks_credentials_and_retry_reuses_mail(self):
        claim,evidence,sub,wizard=self._queued()
        Server=type(self.env['ir.mail_server'])
        with patch.object(Server,'_connect__',side_effect=RuntimeError('SMTP password=secret-do-not-expose')):
            sub.mail_id.sudo().send(auto_commit=False)
        self.assertEqual(sub.state,'failed')
        self.assertNotIn('secret-do-not-expose',sub.last_failure)
        self.assertNotIn('secret-do-not-expose',self.env['mail.mail'].browse(sub.mail_id.id).failure_reason)
        old=sub.mail_id; pdf=sub.pdf_attachment_id.raw.content
        with self.assertRaises(ValidationError): sub.action_retry('Retry after reviewing SMTP configuration')
        with self.assertRaises(ValidationError): sub.action_retry('A string is not an explicit uncertainty acknowledgment','False')
        sub.action_retry('Retry after administrator reviewed SMTP configuration',True)
        self.assertEqual(sub.state,'queued'); self.assertEqual(sub.retry_count,1)
        self.assertEqual(old,sub.mail_id)
        self.assertEqual(pdf,sub.pdf_attachment_id.raw.content)
        self._send_mocked(sub)
        self.assertEqual(sub.state,'sent'); self.assertEqual(sub.delivery_status,'unknown')

    def test_source_change_before_dispatch_blocks_mock_transport(self):
        claim,evidence,sub,wizard=self._queued()
        additional_receipt(claim.purchase_id,5)
        self.assertEqual(claim.approval_status,'stale')
        Server=type(self.env['ir.mail_server'])
        with patch.object(Server,'send_email') as transport:
            sub.mail_id.sudo().send(auto_commit=False)
            transport.assert_not_called()
        self.assertEqual(sub.state,'failed')
        self.assertFalse(sub.sent_at)
        with self.assertRaises(ValidationError): sub.action_retry('Source is not approved; this retry must be blocked',True)

    def test_withdrawn_disclosure_before_dispatch_blocks_transport(self):
        claim,evidence,sub,wizard=self._queued()
        evidence.with_user(self.sender).action_set_external_sharing(False,'Disclosure withdrawn before native queue dispatch')
        sub.mail_id.sudo().send(auto_commit=False)
        self.assertEqual(sub.state,'failed')
        self.assertFalse(sub.sent_at)

    def test_failed_queue_creation_has_no_success_event_or_partial_mail(self):
        claim,evidence,sub,wizard=self._prepared()
        wizard.action_preview(); wizard.confirmed=True
        before=claim.audit_event_ids; before_mail=self.env['mail.mail'].search_count([])
        Mail=type(self.env['mail.mail']); original=Mail.create
        def broken(records, values):
            rows=values if isinstance(values,list) else [values]
            if any(v.get('model')==SUB for v in rows): raise RuntimeError('Simulated queue insertion failure')
            return original(records,values)
        with patch.object(Mail,'create',broken):
            with self.assertRaises(RuntimeError): wizard.action_submit_to_supplier()
        sub.invalidate_recordset(); claim.invalidate_recordset()
        self.assertEqual(sub.state,'ready'); self.assertFalse(sub.mail_id)
        self.assertEqual(before,claim.audit_event_ids)
        self.assertEqual(before_mail,self.env['mail.mail'].search_count([]))

    def test_direct_submission_mail_and_file_mutations_are_blocked(self):
        claim,evidence,sub,wizard=self._queued()
        for action in (lambda:sub.write({'state':'sent'}),lambda:sub.copy(),lambda:sub.unlink(),
            lambda:sub.create({'claim_id':claim.id,'state':'queued'}),lambda:claim.write({'submission_ids':[Command.clear()]}),
            lambda:sub.mail_id.write({'state':'sent'}),lambda:sub.mail_id.write({'email_to':'thief@example.invalid'}),
            lambda:sub.mail_id.unlink(),lambda:sub.mail_id.copy(),lambda:sub.message_id.write({'body':'Forged successful communication'}),
            lambda:sub.message_id.unlink(),lambda:sub.pdf_attachment_id.write({'public':True}),
            lambda:sub.pdf_attachment_id.write({'company_id':self.other.id}),lambda:sub.pdf_attachment_id.unlink(),
            lambda:sub.pdf_attachment_id.copy(),lambda:sub.pdf_attachment_id.generate_access_token()):
            with self.assertRaises(AccessError): action()
        with self.assertRaises(AccessError): self.env['mail.mail'].with_user(self.sender).with_context(ssc_internal_send=True).create({'model':SUB,'res_id':sub.id,'state':'outgoing'})
        with self.assertRaises(AccessError): self.env['mail.message'].with_user(self.sender).create({'model':SUB,'res_id':sub.id,'body':'Forged supplier acceptance'})

    def test_forged_wizard_identity_and_preview_context_blocked(self):
        claim,evidence,sub,wizard=self._prepared()
        with self.assertRaises(AccessError): wizard.write({'submission_id':999999})
        with self.assertRaises(AccessError): wizard.write({'claim_id':999999})
        with self.assertRaises(AccessError): wizard.write({'preview_fingerprint':'forged'})
        with self.assertRaises(AccessError): wizard.create({'submission_id':sub.id,'claim_id':999999,'subject':sub.subject,'body_html':sub.body_html})
        with self.assertRaises(ValidationError): wizard.with_context(preview_fingerprint='forged',confirmed=True).action_submit_to_supplier()
        self.assertFalse(sub.mail_id)

    def test_company_report_download_and_group_counts_are_isolated(self):
        claim,evidence,sub,wizard=self._queued()
        stranger=new_test_user(self.env,login='ssc_submission_company_b',groups=M+'group_claim_manager,'+M+'group_external_sender',
            company_id=self.other.id,company_ids=[Command.set(self.other.ids)])
        with self.assertRaises(AccessError): sub.with_user(stranger).action_open_pdf()
        with self.assertRaises(AccessError): claim.with_user(stranger).action_prepare_supplier_submission()
        with self.assertRaises(AccessError): sub.pdf_attachment_id.with_user(stranger)._to_http_stream()
        report=self.env.ref(M+'report_supplier_claim_statement').with_user(stranger)
        with self.assertRaises(AccessError): report._render_qweb_pdf(report.id,res_ids=sub.ids)
        self.assertFalse(self.env[SUB].with_user(stranger).search([('id','=',sub.id)]))
        self.assertFalse(self.env[SUB].with_user(stranger)._read_group([('id','=',sub.id)],['company_id'],['__count']))

    def test_frozen_historical_pdf_and_evidence_survive_source_changes(self):
        claim,evidence,sub,wizard=self._queued()
        self._send_mocked(sub)
        pdf=sub.pdf_attachment_id.raw.content
        files={a.id:a.raw.content for a in sub.attachment_ids}
        self.company.name='Changed company display name'
        additional_receipt(claim.purchase_id,5)
        report=self.env.ref(M+'report_supplier_claim_statement').with_user(self.sender)
        with patch.object(type(report),'_run_pdf_engine',side_effect=AssertionError('Historical PDF must never be regenerated')):
            historical,kind=report._render_qweb_pdf(report.id,res_ids=sub.ids)
        self.assertEqual(historical,pdf); self.assertEqual(kind,'pdf')
        self.assertEqual(files,{a.id:a.raw.content for a in sub.attachment_ids})
        self.assertEqual(sub.state,'sent'); self.assertEqual(claim.approval_status,'stale')

    def test_responses_and_followup_do_not_accept_or_recover_claim(self):
        claim,evidence,sub,wizard=self._queued(); self._send_mocked(sub)
        sub.action_record_response('acknowledgment','Supplier acknowledged the message; no liability or amount accepted')
        sub.action_record_response('disagreement','Supplier disputes the quantity and asks for receiving details')
        history=sub.communication_ids
        self.assertTrue(history.filtered(lambda c:c.direction=='inbound'))
        with self.assertRaises(AccessError): history.write({'kind':'acknowledgment'})
        with self.assertRaises(AccessError): history.unlink()
        with self.assertRaises(AccessError): sub.with_user(self.user).action_record_response('note','An ordinary preparer cannot forge supplier responses')
        followup=sub.action_prepare_revision('Please clarify the supplier response with supporting files',True)
        again=sub.action_prepare_revision('Retry opening the same pending manual follow-up',True)
        self.assertEqual(followup['res_id'],again['res_id'])
        new=self.env[SUB].with_user(self.sender).browse(followup['res_id'])
        self.assertEqual(new.purpose,'followup'); self.assertFalse(new.mail_id)
        self.assertEqual(claim.state,'under_review'); self.assertEqual(claim.approval_status,'approved')

    def test_controlled_cancel_does_not_delete_history_or_send(self):
        claim,evidence,sub,wizard=self._queued()
        with self.assertRaises(ValidationError): sub.action_cancel()
        self.policy.active=False
        sub.action_cancel('Cancel this pending message before native outgoing processing')
        self.assertEqual(sub.state,'canceled'); self.assertEqual(sub.mail_id.state,'cancel')
        self.assertTrue(sub.pdf_attachment_id)
        with self.assertRaises(ValidationError): sub.action_open_composer()
        self.policy.active=True
        again=claim.with_user(self.sender).action_prepare_supplier_submission()
        self.assertNotEqual(again['res_id'],sub.id)

    def test_material_quantity_revision_requires_new_approval_and_new_pdf(self):
        claim,evidence,sub,wizard=self._queued(); self._send_mocked(sub)
        original_pdf=sub.pdf_attachment_id.raw.content
        claim.action_start_approval_correction('Revise reserved quantity after authorized commercial correction')
        claim.correction_reason='Reduce the allocated claim quantity from fifteen to ten'
        claim.claim_line_ids.proposed_qty=10
        claim.action_revalidate_preparation(); complete_evidence(claim)
        claim.action_submit_for_review()
        with self.assertRaises(ValidationError): claim.with_user(self.sender).action_prepare_supplier_submission()
        claim.with_user(self.reviewer).action_verify_financial_value('Corrected quantity and posted untaxed invoice value reviewed')
        claim.action_request_internal_approval()
        claim.approval_step_ids.filtered(lambda a:a.cycle_id.number==claim.approval_cycle).with_user(self.sender).action_approve()
        action=claim.with_user(self.sender).action_prepare_supplier_submission()
        new=self.env[SUB].with_user(self.sender).browse(action['res_id'])
        wa=new.action_open_composer(); w=self.env['supplier.shortage.claim.send.wizard'].with_user(self.sender).browse(wa['res_id'])
        w.evidence_ids=[Command.set(evidence.ids)]; w.action_preview(); w.confirmed=True; w.action_submit_to_supplier()
        self.assertEqual(new.revision,sub.revision+1); self.assertEqual(new.amount,100)
        self.assertNotEqual(new.approval_id,sub.approval_id)
        self.assertNotEqual(new.pdf_attachment_id.raw.content,original_pdf)
        self.assertEqual(sub.pdf_attachment_id.raw.content,original_pdf)
        self.assertTrue(sub.superseded)

    def test_multiline_multipage_pdf_escape_totals_and_artifact(self):
        self.product.name='Reviewed supplier delivery item with a detailed specification, packaging description and source reference. '*3
        claim,evidence,sub,wizard=self._prepared(products=[self.product]*8,
            reason='Observed shortage <script>UNTRUSTED_MARKER</script> & supplier review requested.\n'+'Delivery detail and quantity reconciliation. '*100)
        wizard.action_preview()
        pdf=sub.pdf_attachment_id.raw.content
        reader=PdfFileReader(io.BytesIO(pdf)); text=' '.join(p.extract_text() for p in reader.pages)
        self.assertGreater(len(reader.pages),1)
        self.assertEqual(sub.amount,1200)
        self.assertEqual(sum(l['amount'] for l in sub.external_snapshot['lines']),sub.amount)
        self.assertIn('1,200.00',text)
        self.assertIn('UNTRUSTED_MARKER',text)
        self.assertTrue(all(float(p.mediabox.width)>590 for p in reader.pages))
        artifacts=Path(__file__).resolve().parents[1]/'.testing/artifacts'
        artifacts.mkdir(exist_ok=True)
        (artifacts/'step13-multipage-claim.pdf').write_bytes(pdf)

    def test_pending_finance_and_rejected_claims_never_prepare(self):
        claim,evidence=self._approved(chain='finance',stop_before='finance')
        self.assertEqual(claim.approval_status,'finance')
        with self.assertRaises(ValidationError): claim.with_user(self.finance).action_prepare_supplier_submission()
        claim.approval_step_ids.filtered(lambda s:s.role=='finance').with_user(self.finance).action_reject('Finance authorization rejected on documented risk review')
        self.assertEqual(claim.approval_status,'rejected')
        with self.assertRaises(ValidationError): claim.with_user(self.finance).action_prepare_supplier_submission()
        self.assertFalse(claim.submission_ids)

    def test_internal_finance_notes_cannot_be_externally_authorized(self):
        claim,evidence,sub,wizard=self._prepared(internal=True)
        private=claim.evidence_ids.filtered(lambda e:e.evidence_type=='internal')
        self.assertEqual(private.effective_state,'verified')
        self.assertFalse(private.attachment_id)
        with self.assertRaises(ValidationError): private.with_user(self.sender).action_set_external_sharing(True,'Internal discussion must never become a supplier attachment')
        wizard.evidence_ids=[Command.set((evidence|private).ids)]
        with self.assertRaises(ValidationError): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_cross_company_supplier_contact_is_rejected(self):
        claim,evidence,sub,wizard=self._prepared()
        wrong=self.env['res.partner'].create({'name':'Company B-only supplier contact','parent_id':self.supplier.id,
            'company_id':self.other.id,'email':'company-b-only@example.invalid'})
        wizard.write({'recipient_partner_id':wrong.id,'recipient_email':wrong.email})
        with self.assertRaises((AccessError,ValidationError)): wizard.action_preview()
        self.assertFalse(sub.mail_id)

    def test_durable_attempt_guard_prevents_blind_redispatch(self):
        claim,evidence,sub,wizard=self._queued()
        Guard=self.env['supplier.shortage.dispatch.attempt']
        self.assertTrue(Guard._reserve(sub,self.env['mail.mail'].browse(sub.mail_id.id)))
        Server=type(self.env['ir.mail_server'])
        with patch.object(Server,'send_email') as transport:
            sub.mail_id.sudo().send(auto_commit=False)
            transport.assert_not_called()
        self.assertEqual(sub.state,'failed')
        self.assertIn('uncertain',sub.last_failure.lower())
        self.assertEqual(sub.delivery_status,'unknown')
        sub.action_retry('Administrator reviewed uncertain dispatch before explicit retry',True)
        self._send_mocked(sub)
        self.assertEqual(sub.state,'sent')

    def test_template_rendering_cannot_trigger_business_actions(self):
        claim,evidence=self._approved()
        self.policy.template_id.with_user(self.configurator).write({'body_html':
            '<div><t t-out="object.claim_id.action_prepare_supplier_submission()"/></div>'})
        before=self.env['mail.mail'].search_count([])
        with self.assertRaises((AccessError,ValidationError,UserError)):
            claim.with_user(self.sender).action_prepare_supplier_submission()
        self.assertFalse(claim.submission_ids)
        self.assertEqual(before,self.env['mail.mail'].search_count([]))

    def test_frozen_copies_do_not_bypass_original_file_permissions(self):
        claim,evidence,sub,wizard=self._queued()
        self.env['ir.access'].create({'name':'Restricted original supplier evidence file',
            'model_id':self.env['ir.model']._get_id('ir.attachment'),'operation':'r',
            'domain':"[] if user.id != %s else [('id','!=',%s)]"%(self.sender.id,evidence.attachment_id.id)})
        with self.assertRaises(AccessError): sub.pdf_attachment_id.with_user(self.sender)._to_http_stream()
        with self.assertRaises(AccessError): sub.with_user(self.sender).action_open_pdf()
        self.assertFalse(self.env[SUB].with_user(self.sender).search([('id','=',sub.id)]))

