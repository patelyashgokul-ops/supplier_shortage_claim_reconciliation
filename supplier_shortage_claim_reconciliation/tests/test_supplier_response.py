import base64
from datetime import timedelta
from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import new_test_user
from odoo.addons.supplier_shortage_claim_reconciliation.models.submission_mail import MAIL_CAPABILITY
from .test_supplier_submission import SupplierSubmissionCase, M, SUB
from .preparation_fixtures import verify_document

R='supplier.shortage.claim.response'
N='supplier.shortage.claim.negotiation'
C='supplier.shortage.claim.commitment'
D='supplier.shortage.claim.dispute'


class TestSupplierResponse(SupplierSubmissionCase):
    """Real approved/sent revisions; non-superuser business decisions; SMTP mocked."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.preparer=new_test_user(cls.env,login='ssc_response_preparer',groups=M+'group_claim_user,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        fixture=cls('test_unverified_does_not_affect_summary')
        cls.common_claim,cls.common_evidence,cls.common_sub,cls.common_wizard=fixture._queued()
        fixture.assertEqual(fixture._send_mocked(cls.common_sub),1)
        cls.common_reason=cls.env['supplier.shortage.response.reason'].with_user(cls.configurator).create({'name':'Supplier disputes delivery quantity'})

    def _validate(self, picking, quantity=None):
        return super()._validate(picking,80 if quantity==85 else quantity)

    def _order(self, *args, **kwargs):
        order=super()._order(*args,**kwargs)
        order.order_line.price_unit=1000
        return order

    def _bill(self, lines, posted=False, **kwargs):
        bill=super()._bill(lines,False,**kwargs)
        bill.invoice_line_ids.price_unit=1000
        if posted: bill.action_post()
        return bill

    def setUp(self):
        super().setUp()
        self.claim,self.evidence,self.sub,self.wizard=self.common_claim,self.common_evidence,self.common_sub,self.common_wizard
        self.reason=self.common_reason
        self.Response=self.env[R].with_user(self.preparer)
        self.Negotiation=self.env[N].with_user(self.user)
        self.Commitment=self.env[C].with_user(self.user)
        self.Dispute=self.env[D].with_user(self.sender)

    def _response(self, accepted=0, rejected=0, disputed=0, sub=None, **changes):
        sub=sub or self.sub
        line=sub.claim_id.claim_line_ids[:1]
        values={'submission_id':sub.id,'supplier_contact_id':self.supplier.id,'response_type':'acceptance' if accepted else 'rejection' if rejected else 'dispute',
            'response_source':'phone','response_reference':'Direct callback to registered supplier contact, recorded discussion reference 14',
            'response_summary':'Supplier contact confirmed the exact claim line quantities during a documented business discussion.',
            'line_ids':[Command.create({'claim_line_id':line.id,'accepted_qty':accepted,'rejected_qty':rejected,'disputed_qty':disputed,
                'rejection_reason_id':self.reason.id if rejected else False,'decision_reason':'Supplier disputes the original delivery record and requires a joint investigation.'})]}
        values.update(changes)
        return self.Response.create(values)

    def _verify(self, response, user=None):
        rec=response.with_user(user or self.sender)
        rec.verification_note='Independently verified supplier identity by direct callback, exact revision, scope and final decision intent.'
        return rec.action_verify()

    def _offer(self, amount=10000, **changes):
        values={'submission_id':self.sub.id,'negotiation_type':'supplier_offer','proposed_amount':amount,'currency_id':self.sub.currency_id.id,
                'tax_basis':'untaxed','description':'Supplier proposes alternative financial consideration; no binding company acceptance is inferred.'}
        values.update(changes)
        return self.Negotiation.create(values)

    def _promise(self, response, **changes):
        values={'submission_id':self.sub.id,'response_id':response.id,'commitment_type':'credit','committed_amount':12000,
            'currency_id':self.sub.currency_id.id,'promised_date':fields.Date.today()+timedelta(days=7),
            'description':'Supplier promises a future credit document; no settlement document has been received or verified.'}
        values.update(changes)
        return self.Commitment.create(values)

    def _dispute(self, **changes):
        values={'submission_id':self.sub.id,'claim_line_id':self.claim.claim_line_ids.id,'issue_reference':'Delivery quantity discrepancy',
                'description':'Supplier disputes the delivery quantity; warehouse and purchase teams must jointly investigate.'}
        values.update(changes)
        return self.Dispute.create(values)

    def _fresh_claim(self):
        return self.claim.with_user(self.sender).with_context(allowed_company_ids=self.company.ids)

    def test_full_acceptance_no_recovery_or_source_mutation(self):
        sources=('account.move','account.move.line','account.payment','stock.move','stock.quant','mail.mail')
        before={m:self.env[m].search_count([]) for m in sources}
        dates=(self.claim.purchase_id.write_date,self.claim.purchase_id.order_line.write_date,self.claim.purchase_id.picking_ids.mapped('write_date'),self.claim.purchase_id.order_line.invoice_lines.move_id.mapped('write_date'))
        rec=self._response(20); self._verify(rec)
        c=self._fresh_claim()
        self.assertEqual((c.supplier_approved_amount,c.supplier_accepted_amount,c.supplier_pending_amount,c.verified_recovery_amount),(20000,20000,0,0))
        self.assertEqual(c.supplier_response_status,'accepted')
        self.assertEqual(c.state,'under_review')
        self.assertEqual(before,{m:self.env[m].search_count([]) for m in sources})
        self.assertEqual(dates,(c.purchase_id.write_date,c.purchase_id.order_line.write_date,c.purchase_id.picking_ids.mapped('write_date'),c.purchase_id.order_line.invoice_lines.move_id.mapped('write_date')))

    def test_unverified_does_not_affect_summary(self):
        rec=self._response(20)
        self.assertFalse(rec.current_key)
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,0)
        self.assertEqual(self._fresh_claim().supplier_pending_amount,20000)
        self.assertEqual(rec.reported_by,self.preparer)
        self.assertEqual(rec.communication_id.submission_id,self.sub)

    def test_partial_20_12_8_reconciles(self):
        rec=self._response(12,disputed=8); self._verify(rec)
        self.assertEqual((rec.accepted_amount,rec.disputed_amount,rec.pending_amount),(12000,8000,0))
        self.assertEqual(rec.line_ids.pending_qty,0)
        c=self._fresh_claim()
        self.assertEqual(c.supplier_response_status,'partial')
        self.assertEqual(c.supplier_approved_amount,c.supplier_accepted_amount+c.supplier_rejected_amount+c.supplier_disputed_amount+c.supplier_pending_amount)
        self.assertEqual(c.verified_recovery_amount,0)

    def test_unanswered_quantity_is_pending(self):
        rec=self._response(12); self._verify(rec)
        self.assertEqual((rec.line_ids.pending_qty,rec.pending_amount,rec.rejected_amount),(8,8000,0))

    def test_overallocation_negative_and_nonfinite_rejected(self):
        for args in ({'accepted':21},{'accepted':12,'rejected':9},{'accepted':-1},{'disputed':float('nan')},{'accepted':float('inf')}):
            with self.subTest(args=args),self.assertRaises(ValidationError),self.cr.savepoint():
                self._response(**args)

    def test_full_and_partial_rejection_preserve_internal_claim(self):
        for accepted,rejected in ((0,20),(12,8)):
            rec=self._response(accepted,rejected); self._verify(rec)
            self.assertEqual(rec.accepted_amount+rec.rejected_amount,20000)
            self.assertEqual(self.claim.state,'under_review')
            self.assertEqual(self._fresh_claim().verified_recovery_amount,0)

    def test_rejection_requires_reason_and_explanation(self):
        for changes in ({'rejection_reason_id':False},{'decision_reason':'No'}):
            values={'claim_line_id':self.claim.claim_line_ids.id,'rejected_qty':20,'rejection_reason_id':self.reason.id,
                    'decision_reason':'Supplier explicitly disputes the delivered quantity and shipment provenance.'}
            values.update(changes)
            with self.assertRaises(ValidationError),self.cr.savepoint():
                self._response(line_ids=[Command.create(values)])

    def test_direct_forged_verification_and_related_fields_denied(self):
        for key,value in [('verification_state','verified'),('verified_by',self.sender.id),('company_id',self.other.id),('claim_id',self.claim.id),('accepted_amount',20000),('reported_by',self.sender.id),('current_key',str(self.sub.id))]:
            with self.subTest(field=key),self.assertRaises(AccessError),self.cr.savepoint(): self._response(**{key:value})
        rec=self._response(12)
        for values in ({'verification_state':'verified'},{'supplier_contact_id':self.supplier.id},{'accepted_amount':20000}):
            with self.assertRaises(AccessError),self.cr.savepoint(): rec.write(values)
        with self.assertRaises(AccessError),self.cr.savepoint(): rec.line_ids.write({'accepted_qty':20})

    def test_context_defaults_cannot_forge_decision(self):
        rec=self.Response.with_context(default_verification_state='verified',default_current_key=str(self.sub.id),default_verified_by=self.sender.id).create({
            'submission_id':self.sub.id,'supplier_contact_id':self.supplier.id,'response_type':'acknowledgment','response_source':'phone',
            'response_reference':'Supplier callback confirmed receipt only','response_summary':'Supplier acknowledges receipt only and makes no decision on any claim quantity.'})
        self.assertEqual(rec.verification_state,'unverified'); self.assertFalse(rec.current_key); self.assertFalse(rec.verified_by)

    def test_independent_and_authorized_reviewer_required(self):
        rec=self._response(20)
        with self.assertRaises(AccessError),self.cr.savepoint(): rec.action_verify()
        own=self.Response.with_user(self.sender).create({'submission_id':self.sub.id,'supplier_contact_id':self.supplier.id,'response_type':'acknowledgment',
            'response_source':'phone','response_reference':'Independent direct contact callback','response_summary':'Supplier confirmed receipt of the claim communication only.'})
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(own)

    def test_acknowledgment_not_acceptance_and_does_not_erase_position(self):
        first=self._response(12); self._verify(first)
        ack=self._response(line_ids=[],response_type='acknowledgment'); self._verify(ack)
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,12000)
        self.assertTrue(first.current_key); self.assertFalse(ack.current_key)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._response(20,response_type='acknowledgment')

    def test_revised_acceptance_replaces_total_and_preserves_history(self):
        first=self._response(10); self._verify(first)
        second=self._response(15); self._verify(second)
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,15000)
        self.assertEqual(first.verification_state,'superseded'); self.assertEqual(first.superseded_by_id,second)
        self.assertFalse(first.current_key)
        self.assertEqual(first.line_ids.accepted_qty,10)
        self.assertTrue(self.claim.audit_event_ids.filtered(lambda a:'prior position %s' % first.id in a.change_summary))

    def test_reduction_requires_finance_authority(self):
        first=self._response(15); self._verify(first)
        lower=self._response(10)
        with self.assertRaises(AccessError),self.cr.savepoint(): self._verify(lower)
        self.assertEqual(first.verification_state,'verified')
        self._verify(lower,self.finance)
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,10000)

    def test_processing_is_idempotent_and_history_immutable(self):
        rec=self._response(12); self._verify(rec)
        count=len(self.claim.audit_event_ids)
        rec.with_user(self.sender).action_verify()
        self.assertEqual(len(self.claim.audit_event_ids),count)
        for obj in (rec,rec.line_ids):
            with self.assertRaises(AccessError),self.cr.savepoint(): obj.unlink()
            with self.assertRaises(AccessError),self.cr.savepoint(): obj.copy()

    def test_wrong_supplier_and_line_scope_rejected(self):
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._response(supplier_contact_id=self.company.partner_id.id)
        other_claim,_,other_sub,_=self._queued()
        self._send_mocked(other_sub)
        with self.assertRaises(ValidationError),self.cr.savepoint():
            self._response(line_ids=[Command.create({'claim_line_id':other_claim.claim_line_ids.id,'accepted_qty':1})])

    def test_duplicate_exact_lines_rejected(self):
        with self.assertRaises(ValidationError),self.cr.savepoint():
            self._response(line_ids=[Command.create({'claim_line_id':self.claim.claim_line_ids.id,'accepted_qty':5}) for i in range(2)])

    def test_multiline_same_product_po_identity(self):
        claim,_,sub,_=self._queued(products=[self.product,self.product]); self._send_mocked(sub)
        a,b=claim.claim_line_ids
        rec=self._response(sub=sub,line_ids=[Command.create({'claim_line_id':a.id,'accepted_qty':20}),
            Command.create({'claim_line_id':b.id,'accepted_qty':12,'rejected_qty':8,'rejection_reason_id':self.reason.id,
                            'decision_reason':'Supplier disputes this separate purchase line delivery quantity.'})])
        self._verify(rec)
        self.assertEqual(len(rec.line_ids.purchase_line_id),2)
        self.assertEqual((rec.accepted_amount,rec.rejected_amount,rec.pending_amount),(32000,8000,0))

    def test_supplier_documents_after_submission_reuse_secure_evidence(self):
        original=self.sub.approval_id.fingerprint
        evidence=self._supplier_document()
        self.assertEqual(self.claim.approval_status,'approved')
        self.assertEqual(self.sub.approval_id.fingerprint,original)
        rec=self._response(20,response_source='document',evidence_ids=[Command.set(evidence.ids)])
        self._verify(rec)
        self.assertEqual(rec.verification_state,'verified')
        self.assertEqual(evidence.attachment_id.company_id,self.company)
        self.assertFalse(evidence.attachment_id.public)
        with self.assertRaises(AccessError),self.cr.savepoint(): evidence.attachment_id.generate_access_token()

    def _supplier_document(self):
        evidence=self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({'claim_id':self.claim.id,
            'supplier_response_submission_id':self.sub.id,'evidence_type':'supplier_response','description':'Supplier signed shortage response',
            'scope_line_ids':[Command.set(self.claim.claim_line_ids.ids)],'filename':'supplier-response.txt',
            'upload_data':base64.b64encode(b'Supplier signed statement about exact submitted quantities; test evidence only.'),
            'verification_note':'Independent authenticity, supplier identity and signed document scope checked against the original source.'})
        verify_document(evidence)
        return evidence

    def _email(self, sender=None, parent=None):
        # Simulate a trusted native intake, never enable a public RPC bypass.
        token=MAIL_CAPABILITY.set(True)
        try:
            return self.env['mail.message'].create({'model':SUB,'res_id':self.sub.id,'message_type':'email','email_from':sender or self.supplier.email,
                'parent_id':(parent or self.sub.message_id).id,'subject':'Supplier decision response','body':'Supplier response is evidence only.'})
        finally:
            MAIL_CAPABILITY.reset(token)

    def test_native_email_traceability_duplicate_and_no_auto_acceptance(self):
        msg=self._email()
        rec=self._response(12,response_source='email',source_message_id=msg.id,evidence_ids=[Command.set(self._supplier_document().ids)])
        self.assertEqual(rec.communication_id.mail_message_id,msg)
        count=len(self.claim.communication_ids)
        duplicate=self._response(20,response_source='email',source_message_id=msg.id)
        self.assertEqual(rec,duplicate); self.assertEqual(len(self.claim.communication_ids),count)
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,0)
        self._verify(rec); self.assertEqual(self._fresh_claim().supplier_accepted_amount,12000)

    def test_wrong_email_sender_requires_review(self):
        rec=self._response(20,response_source='email',source_message_id=self._email('impostor@example.invalid').id)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(rec)
        rec.with_user(self.sender).action_require_review()
        self.assertEqual(rec.verification_state,'review'); self.assertFalse(rec.current_key)

    def test_email_wrong_thread_and_missing_identity_evidence(self):
        rec=self._response(20,response_source='email',source_message_id=self._email().id)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(rec)
        other=self.env['mail.message'].create({'body':'Unrelated message'})
        wrong=self._response(20,response_source='email',source_message_id=self._email(parent=other).id)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(wrong)

    def test_document_without_provenance_cannot_be_verified(self):
        rec=self._response(20,response_source='document')
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(rec)
        rec.with_user(self.sender).action_require_review()
        self.assertEqual(rec.verification_state,'review')

    def test_contradictory_supplier_evidence_flags_reapproval(self):
        evidence=self._supplier_document()
        evidence.with_user(self.sender).action_flag_claim_contradiction()
        self.assertEqual(self.claim.approval_status,'stale')
        rec=self._response(20)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(rec)

    def test_offers_historical_distinct_and_not_acceptance(self):
        first=self._offer(10000)
        counter=self._offer(17000,negotiation_type='counteroffer')
        final=self._offer(15000,negotiation_type='revised',supersedes_event_id=first.id)
        self.assertEqual([first.proposed_amount,counter.proposed_amount,final.proposed_amount],[10000,17000,15000])
        c=self._fresh_claim(); self.assertEqual(c.current_offer_id,final)
        self.assertEqual(c.supplier_accepted_amount,0); self.assertEqual(c.verified_recovery_amount,0)
        self.assertTrue(final.financial_review_required)

    def test_offer_authority_accounts_then_independent_finance(self):
        offer=self._offer()
        with self.assertRaises(AccessError),self.cr.savepoint(): offer.action_authorize()
        with self.assertRaises(ValidationError),self.cr.savepoint(): offer.with_user(self.finance).action_authorize()
        accounts=offer.with_user(self.reviewer)
        accounts.decision_note='Reviewed untaxed proposal against posted bill valuation and identified the commercial concession.'
        accounts.action_accounts_review()
        offer.with_user(self.finance).action_authorize()
        self.assertEqual(offer.status,'authorized'); self.assertEqual(self.claim.verified_amount,20000)
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,0)

    def test_currency_and_tax_ambiguous_offers_review_only(self):
        other=self.env['res.currency'].search([('id','!=',self.sub.currency_id.id),('active','=',True)],limit=1)
        if not other: other=self.env.ref('base.EUR')
        for changes in ({'currency_id':other.id},{'tax_basis':'inclusive'},{'tax_basis':'unknown'}):
            offer=self._offer(**changes)
            self.assertTrue(offer.financial_review_required)
            offer.with_user(self.reviewer).decision_note='Accounts reviewed original foreign currency or ambiguous tax basis; normalization is still required.'
            offer.with_user(self.reviewer).action_accounts_review()
            with self.assertRaises(ValidationError),self.cr.savepoint(): offer.with_user(self.finance).action_authorize()
        self.assertEqual(self._fresh_claim().supplier_accepted_amount,0)

    def test_offer_terms_and_status_cannot_be_forged(self):
        with self.assertRaises(AccessError),self.cr.savepoint(): self._offer(status='authorized')
        offer=self._offer()
        for changes in ({'status':'authorized'},{'proposed_amount':20000},{'accounts_by':self.reviewer.id}):
            with self.assertRaises(AccessError),self.cr.savepoint(): offer.write(changes)

    def test_credit_replacement_refund_promises_do_not_execute(self):
        rec=self._response(12); self._verify(rec)
        counts={m:self.env[m].search_count([]) for m in ('account.move','account.payment','stock.move','mail.mail')}
        for kind in ('credit','replacement','refund'):
            promise=self._promise(rec,commitment_type=kind)
            self.assertEqual(promise.status,'promised')
        self.assertEqual(counts,{m:self.env[m].search_count([]) for m in counts})
        self.assertEqual(self._fresh_claim().verified_recovery_amount,0)

    def test_overdue_promise_not_fulfilled_by_date(self):
        rec=self._response(12); self._verify(rec)
        promise=self._promise(rec,promised_date=fields.Date.today()-timedelta(days=1))
        self.assertTrue(promise.overdue); self.assertEqual(promise.status,'promised')
        self.assertEqual(self._fresh_claim().verified_recovery_amount,0)
        promise.with_user(self.sender).status_note='Supplier reports fulfillment; original financial source documents remain unverified.'
        promise.with_user(self.sender).action_pending_verification()
        self.assertEqual(promise.status,'pending'); self.assertFalse(promise.overdue)
        self.assertEqual(self._fresh_claim().verified_recovery_amount,0)

    def test_revised_promise_not_double_counted(self):
        rec=self._response(15); self._verify(rec)
        first=self._promise(rec,committed_amount=10000)
        second=self._promise(rec,committed_amount=15000,supersedes_id=first.id)
        self.assertEqual(first.status,'revised'); self.assertEqual(second.status,'promised')
        self.assertEqual(self._fresh_claim().promised_amount,15000)

    def test_commitment_scope_and_status_guard(self):
        rec=self._response(12)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._promise(rec)
        self._verify(rec)
        for changes in ({'committed_amount':-1},{'committed_qty':21,'claim_line_id':self.claim.claim_line_ids.id,'product_uom_id':self.product.uom_id.id}):
            with self.assertRaises(ValidationError),self.cr.savepoint(): self._promise(rec,**changes)
        with self.assertRaises(AccessError),self.cr.savepoint(): self._promise(rec,status='pending')

    def test_dispute_assignment_deadline_and_resolution(self):
        dispute=self._dispute(next_action_date=fields.Date.today()-timedelta(days=1))
        self.assertTrue(dispute.overdue); self.assertFalse(dispute.contractual_deadline)
        dispute.write({'assigned_user_id':self.finance.id,'next_action':'Finance review of supplier quantity disagreement'})
        dispute.action_investigate(); dispute.action_negotiate(); dispute.action_escalate()
        with self.assertRaises(ValidationError),self.cr.savepoint(): dispute.action_resolve()
        dispute.resolution_summary='Joint review completed and commercial next steps documented; no settlement is recorded.'
        dispute.action_resolve(); dispute.action_close()
        self.assertEqual(dispute.dispute_state,'closed'); self.assertFalse(dispute.active_issue_key)
        self.assertEqual(self.claim.state,'under_review'); self.assertEqual(self._fresh_claim().verified_recovery_amount,0)

    def test_duplicate_dispute_and_invalid_deadline_rejected(self):
        self._dispute()
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._dispute()
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._dispute(issue_reference='Contract terms',contractual_deadline=fields.Date.today())
        other=self._dispute(issue_reference='Contract terms',contractual_deadline=fields.Date.today(),deadline_basis='Actual signed supplier agreement paragraph 14, explicit date; company business calendar considered.')
        self.assertEqual(other.contractual_deadline,fields.Date.today())

    def test_separate_line_disputes_and_reopen_review(self):
        claim,_,sub,_=self._queued(products=[self.product,self.product]); self._send_mocked(sub)
        a,b=claim.claim_line_ids
        first=self._dispute(submission_id=sub.id,claim_line_id=a.id)
        second=self._dispute(submission_id=sub.id,claim_line_id=b.id)
        first.resolution_summary='Additional supplier delivery evidence received; further internal source verification required.'
        first.action_resolve(); first.action_investigate()
        self.assertEqual(second.dispute_state,'open'); self.assertEqual(first.dispute_state,'investigation')

    def test_dispute_unauthorized_edits_and_resolution_denied(self):
        dispute=self._dispute()
        for method in ('action_resolve','action_escalate'):
            with self.assertRaises(AccessError),self.cr.savepoint(): getattr(dispute.with_user(self.preparer),method)()
        with self.assertRaises(AccessError),self.cr.savepoint(): dispute.write({'dispute_state':'closed'})
        with self.assertRaises(AccessError),self.cr.savepoint(): dispute.with_user(self.preparer).write({'assigned_user_id':self.preparer.id})

    def test_failed_verification_no_false_success_audit(self):
        rec=self._response(20,response_source='document')
        before=len(self.claim.audit_event_ids)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(rec)
        self.assertEqual(len(self.claim.audit_event_ids),before)
        self.assertEqual(rec.verification_state,'unverified'); self.assertFalse(rec.current_key)

    def test_company_isolation_response_evidence_and_case_models(self):
        foreign=self.env['res.users'].create({'name':'Other company employee','login':'ssc_response_company_b','company_id':self.other.id,
            'company_ids':[Command.set(self.other.ids)],'group_ids':[Command.set((self.env.ref(M+'group_claim_manager')|self.env.ref(M+'group_external_sender')).ids)]})
        rec=self._response(20); evidence=self._supplier_document(); offer=self._offer(); dispute=self._dispute()
        for obj in (rec,rec.line_ids,evidence,evidence.attachment_id,offer,dispute):
            restricted=obj.with_user(foreign).with_context(allowed_company_ids=self.other.ids)
            self.assertFalse(restricted.search([('id','in',obj.ids)]))
            with self.assertRaises(AccessError),self.cr.savepoint(): restricted.read(['id'])

    def test_views_and_claim_summary_native_load(self):
        for model in (R,N,C,D):
            views=self.env['ir.ui.view'].search([('model','=',model),('type','in',('form','list','search'))])
            self.assertTrue(views)
            for view in views:
                result=self.env[model].with_user(self.sender).get_view(view.id,view.type)
                self.assertTrue(result['arch'])
        result=self.claim.with_user(self.sender).get_view(self.env.ref(M+'view_claim_form').id,'form')
        self.assertIn('supplier_accepted_amount',result['arch'])

    def test_new_supplier_evidence_cannot_forge_revision_or_company(self):
        with self.assertRaises(ValidationError),self.cr.savepoint():
            self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({'claim_id':self.claim.id,
                'supplier_response_submission_id':self.sub.id,'evidence_type':'internal','statement':'Employee assertion is not supplier evidence',
                'description':'Invalid purported supplier evidence'})

    def test_superseded_submission_response_not_effective(self):
        rec=self._response(12)
        self.sub.with_user(self.sender).action_prepare_revision('Corrected claim statement requires renewed explicit supplier confirmation')
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._verify(rec)

    def test_invalid_response_review_and_audit(self):
        rec=self._response(20,response_source='document')
        reviewer=rec.with_user(self.sender)
        reviewer.action_require_review()
        reviewer.verification_note='Original supplier identity and signed document authenticity cannot be established.'
        reviewer.action_invalidate()
        self.assertEqual(rec.verification_state,'invalid'); self.assertFalse(rec.current_key)
        with self.assertRaises(ValidationError),self.cr.savepoint(): reviewer.action_verify()
