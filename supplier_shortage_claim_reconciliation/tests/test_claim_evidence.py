import base64
from copy import deepcopy
from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from .test_claim_preparation import PreparationCase
from .preparation_fixtures import independent_checker, verify_document, complete_evidence


class TestClaimEvidence(PreparationCase):
    def _claim_ready(self, products=None):
        claim=self._prepare(self._candidates(products=products))
        claim.shortage_reason='Delivery shortage supported by native sources and independent checks'
        complete_evidence(claim)
        return claim

    def _file(self,claim,line=None,body=b'Delivery count reconciled against native receipts',name='count.txt'):
        return self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({'claim_id':claim.id,
            'claim_line_id':line.id if line else False,'scope_line_ids':[Command.set((line or claim._review_lines()).ids)],
            'evidence_type':'count','description':'Delivery-specific count document','filename':name,'upload_data':base64.b64encode(body),
            'verification_note':'Independently assessed exact purchase and delivery line scope'})

    def test_independent_checker_and_reviewer_roles(self):
        claim=self._claim_ready()
        evidence=self._file(claim)
        with self.assertRaises(AccessError): evidence.action_verify()
        self.user.write({'group_ids':[Command.link(self.env.ref('supplier_shortage_claim_reconciliation.group_warehouse_reviewer').id)]})
        with self.assertRaises(AccessError): evidence.action_verify()
        verify_document(evidence)
        self.assertEqual(evidence.effective_state,'verified')
        self.assertNotEqual(evidence.verified_by,evidence.create_uid)
        self.assertTrue(evidence.verified_at)
        billing=claim.evidence_ids.filtered(lambda e:e.evidence_type=='bill')
        self.assertEqual(billing.effective_state,'verified')
        self.assertTrue(billing.verified_by.has_group('supplier_shortage_claim_reconciliation.group_accounts_reviewer'))
        checker=independent_checker(self.env,self.company)
        accounts=self.env.ref('supplier_shortage_claim_reconciliation.group_accounts_reviewer')
        warehouse=self.env.ref('supplier_shortage_claim_reconciliation.group_warehouse_reviewer')
        checker.write({'group_ids':[Command.unlink(accounts.id)]})
        with self.assertRaises(AccessError): billing.with_user(checker).action_verify()
        checker.write({'group_ids':[Command.link(accounts.id),Command.unlink(warehouse.id)]})
        billing.with_user(checker).action_verify()
        with self.assertRaises(AccessError): evidence.with_user(checker).action_verify()
        for values in ({'is_current':False},{'reviewed_fingerprint':'fake'},{'verified_by':self.user.id},{'verification_state':'verified'}):
            with self.assertRaises(AccessError): evidence.write(values)

    def test_revision_preserves_file_review_and_rejection_history(self):
        claim=self._claim_ready()
        first=self._file(claim)
        checker=independent_checker(self.env,self.company)
        first.with_user(checker).action_reject()
        rejection=first.rejection_reason
        history=claim.audit_event_ids
        old_attachment=first.attachment_id
        self.assertEqual(old_attachment.raw.content,b'Delivery count reconciled against native receipts')
        self.assertGreater(old_attachment.file_size,0)
        action=first.action_new_revision({'upload_data':base64.b64encode(b'Corrected delivery count explanation'),'filename':'corrected.txt'},'Corrected initial document quantity interpretation')
        second=first.browse(action['res_id'])
        self.assertEqual(second.revision_number,2)
        self.assertEqual(second.supersedes_id,first)
        self.assertFalse(first.is_current)
        self.assertEqual(first.rejection_reason,rejection)
        self.assertEqual(first.attachment_id,old_attachment)
        self.assertNotEqual(second.attachment_id,old_attachment)
        self.assertEqual(second.attachment_id.raw.content,b'Corrected delivery count explanation')
        self.assertEqual(second.effective_state,'not_reviewed')
        self.assertTrue(history<=claim.audit_event_ids)
        second.verification_note='Corrected quantity and scope independently reviewed'
        verify_document(second)
        self.assertEqual(second.effective_state,'verified')
        with self.assertRaises(ValidationError): first.write({'description':'Erase historical rejection'})

    def test_verified_revision_is_not_copied_to_replacement(self):
        claim=self._claim_ready()
        first=self._file(claim)
        verify_document(first)
        actor,stamp=first.verified_by,first.verified_at
        action=first.action_new_revision({'upload_data':base64.b64encode(b'New receiving evidence revision'),'filename':'new.txt'},'New independent physical count document provided')
        second=first.browse(action['res_id'])
        self.assertEqual(first.verified_by,actor)
        self.assertEqual(first.verified_at,stamp)
        self.assertEqual(first.effective_state,'superseded')
        self.assertFalse(second.verified_by)
        claim.action_evaluate_readiness()
        self.assertNotEqual(claim.readiness_status,'ready')

    def test_exact_line_scope_and_unrelated_native_references(self):
        claim=self._claim_ready(products=[self.product,self.second])
        first,second=claim._review_lines()
        doc=self._file(claim,first)
        verify_document(doc)
        self.assertIn(first,doc._scope_lines())
        self.assertNotIn(second,doc._scope_lines())
        foreign=self._prepare(self._candidates(products=[self.product]))
        with self.assertRaises(ValidationError),self.cr.savepoint():
            doc.create({'claim_id':claim.id,'evidence_type':'purchase','source_purchase_id':claim.purchase_id.id,
                'description':'Wrong claim line','scope_line_ids':[Command.set(foreign.claim_line_ids.ids)]})
        move=first.detection_id.source_move_ids.filtered(lambda m:m.state=='done')[:1]
        linked=doc.create({'claim_id':claim.id,'claim_line_id':first.id,'evidence_type':'receipt',
            'description':'Exact receipt movement','source_move_id':move.id})
        linked.verification_note='Exact receipt movement and physical delivery verified'
        verify_document(linked)
        self.assertEqual(linked.effective_state,'verified')
        with self.assertRaises(ValidationError),self.cr.savepoint():
            doc.create({'claim_id':claim.id,'evidence_type':'receipt','description':'Movement cannot prove unrelated product',
                'source_move_id':move.id,'scope_line_ids':[Command.set(claim.claim_line_ids.ids)]})

    def test_file_content_validation_unsafe_size_and_orphan_prevention(self):
        claim=self._claim_ready()
        before=self.env['ir.attachment'].search_count([])
        for data,name in [(b'<html><script>bad</script></html>','fake.pdf'),(b'<svg>active</svg>','bad.svg'),(b'<html>active</html>','bad.txt'),(b'MZ\x00 executable','photo.png')]:
            with self.subTest(name=name),self.assertRaises(ValidationError),self.cr.savepoint(): self._file(claim,body=data,name=name)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._file(claim,body=b'x'*(11*1024*1024))
        self.assertEqual(self.env['ir.attachment'].search_count([]),before)
        valid=self._file(claim)
        with self.assertRaises(AccessError): valid.attachment_id.write({'public':True})
        with self.assertRaises(AccessError): valid.attachment_id.generate_access_token()
        with self.assertRaises(AccessError): valid.attachment_id.write({'datas':base64.b64encode(b'Changed')})

    def test_complete_readiness_submission_and_no_duplicate_check_audit(self):
        claim=self._claim_ready()
        claim.action_evaluate_readiness()
        self.assertEqual(claim.readiness_status,'ready')
        history=claim.audit_event_ids
        claim.action_evaluate_readiness()
        self.assertEqual(claim.audit_event_ids,history)
        claim.action_submit_for_review()
        self.assertEqual(claim.state,'under_review')
        with self.assertRaises(AccessError): claim.write({'readiness_status':'ready'})
        with self.assertRaises(AccessError): claim.claim_line_ids.write({'readiness_status':'ready'})

    def test_missing_accounting_or_warehouse_blocks(self):
        claim=self._prepare(self._candidates())
        claim.shortage_reason='Documented claim still missing independent evidence'
        claim.action_evaluate_readiness()
        self.assertNotEqual(claim.readiness_status,'ready')
        self.assertIn('Warehouse',claim.claim_line_ids.readiness_message)
        self.assertIn('Accounting',claim.claim_line_ids.readiness_message)
        before=claim.audit_event_ids
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        self.assertEqual(claim.state,'draft')
        self.assertEqual(claim.audit_event_ids,before)

    def test_native_receipt_change_invalidates_only_affected_claim(self):
        first=self._claim_ready()
        other=self._claim_ready()
        first.action_evaluate_readiness();other.action_evaluate_readiness()
        physical=self._file(first)
        verify_document(physical)
        first.action_evaluate_readiness()
        from .preparation_fixtures import additional_receipt
        additional_receipt(first.purchase_id,5)
        self.assertTrue(first.readiness_dirty)
        self.assertNotEqual(first.readiness_status,'ready')
        self.assertEqual(physical.effective_state,'requires_reverification')
        self.assertFalse(other.readiness_dirty)
        self.assertEqual(other.readiness_status,'ready')
        with self.assertRaises(ValidationError),self.cr.savepoint(): first.action_submit_for_review()

    def test_bill_cancellation_does_not_stale_unrelated_physical_document(self):
        claim=self._claim_ready()
        physical=self._file(claim)
        verify_document(physical)
        claim.action_evaluate_readiness()
        bill=claim.purchase_id.invoice_ids[:1].with_user(self.env.user)
        bill.button_draft();bill.button_cancel()
        self.assertEqual(physical.effective_state,'verified')
        accounting=claim.evidence_ids.filtered(lambda e:e.evidence_type=='bill')
        self.assertEqual(accounting.effective_state,'requires_reverification')
        self.assertNotEqual(claim.readiness_status,'ready')

    def test_quantity_change_invalidates_scoped_reviews(self):
        claim=self._claim_ready()
        claim.action_evaluate_readiness()
        claim.correction_reason='Corrected proposed quantity after internal investigation'
        claim.claim_line_ids.proposed_qty=10
        self.assertTrue(claim.readiness_dirty)
        self.assertTrue(claim.evidence_ids.filtered('is_stale'))
        claim.action_evaluate_readiness()
        self.assertNotEqual(claim.readiness_status,'ready')

    def test_unrecorded_five_units_blocks_fifteen_reserved_without_stock_changes(self):
        claim=self._claim_ready()
        line=claim.claim_line_ids
        before=line.detection_id.source_move_ids.read(['state','quantity','product_uom_qty','write_date'])
        quants=self.env['stock.quant'].search([('product_id','=',self.product.id)])
        balances=quants.read(['quantity','reserved_quantity','write_date'])
        original=claim.warehouse_observation_ids.filtered('is_current')
        result=original.action_replace({'observation_type':'additional_unrecorded','observed_qty':5,'count_method':'physical',
            'explanation':'Five additional units identified for this delivery; native receipt correction is required.'},'Additional delivery-specific stock found during independent recount')
        observed=self.env['supplier.shortage.warehouse.observation'].with_user(self.user).browse(result['res_id'])
        observed.verification_note='Independent warehouse check confirms five unrecorded units for this receipt'
        observed.with_user(independent_checker(self.env,self.company)).action_verify()
        self.assertTrue(observed.source_conflict)
        self.assert_qty(observed.indicative_supported_qty,10)
        claim.action_evaluate_readiness()
        self.assertEqual(claim.readiness_status,'blocked')
        self.assert_qty(line.allocated_qty,15)
        self.assert_qty(line.supported_qty,10)
        self.assert_qty(line.detection_id.net_received_qty,85)
        self.assertEqual(before,line.detection_id.source_move_ids.read(['state','quantity','product_uom_qty','write_date']))
        self.assertEqual(balances,quants.read(['quantity','reserved_quantity','write_date']))

    def test_observation_wrong_receipt_and_self_check_denied(self):
        claim=self._claim_ready()
        original=claim.warehouse_observation_ids.filtered('is_current')
        self.user.write({'group_ids':[Command.link(self.env.ref('supplier_shortage_claim_reconciliation.group_warehouse_reviewer').id)]})
        # The original observation was made by this user and verified by a separate checker.
        with self.assertRaises(AccessError): original.action_verify()
        other=self._claim_ready()
        with self.assertRaises(ValidationError),self.cr.savepoint():
            original.action_replace({'explanation':'An unrelated receipt cannot establish this delivery count',
                'evidence_ids':[Command.set(other.evidence_ids.ids)]},'Attempt to use unrelated supporting scope must fail')

    def test_source_statement_is_not_a_fabricated_grn(self):
        claim=self._claim_ready()
        evidence=self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({'claim_id':claim.id,
            'evidence_type':'internal','description':'Internal investigation conclusion','statement':'The shortage explanation refers to the exact native PO and receiving provenance.',
            'scope_line_ids':[Command.set(claim.claim_line_ids.ids)],'verification_note':'Independent purchase manager assessed statement relevance'})
        verify_document(evidence)
        self.assertFalse(evidence.attachment_id)
        self.assertFalse(evidence.source_picking_id)
        self.assertEqual(evidence.effective_state,'verified')

    def test_dynamic_requirement_and_independent_documented_exception(self):
        claim=self._claim_ready()
        config=new_test_user(self.env,login='ssc_evidence_policy_admin',groups='supplier_shortage_claim_reconciliation.group_module_admin',
            company_id=self.company.id,company_ids=[Command.set(self.company.ids)])
        rule=self.env['supplier.shortage.evidence.requirement'].with_user(config).create({'name':'Additional supplier confirmation',
            'company_id':self.company.id,'scenario':'billed_unreceived','category':'communication','allow_exception':True})
        claim.action_evaluate_readiness()
        self.assertNotEqual(claim.readiness_status,'ready')
        request=self.env['supplier.shortage.evidence.exception'].with_user(self.user).create({'claim_id':claim.id,
            'claim_line_id':claim.claim_line_ids.id,'requirement_id':rule.id,'reason':'Supplier communication is unavailable; verified native sources and warehouse report support internal investigation.'})
        with self.assertRaises(ValidationError): request.action_authorize()
        request.with_user(independent_checker(self.env,self.company)).action_authorize()
        claim.action_evaluate_readiness()
        self.assertEqual(claim.readiness_status,'ready')
        with self.assertRaises(AccessError): rule.with_user(self.user).write({'active':False})

    def test_one_blocked_line_prevents_ready_claim(self):
        claim=self._claim_ready(products=[self.product,self.second])
        target=claim.claim_line_ids[-1]
        observation=claim.warehouse_observation_ids.filtered(lambda o:o.claim_line_id==target)
        observation.with_user(independent_checker(self.env,self.company)).action_reject()
        claim.action_evaluate_readiness()
        self.assertNotEqual(claim.readiness_status,'ready')
        self.assertNotEqual(target.readiness_status,'ready')
        self.assertEqual(claim.claim_line_ids[0].readiness_status,'ready')

    def test_workspace_and_revision_views_load(self):
        claim=self._claim_ready()
        evidence=claim.evidence_ids[:1]
        for model,xmlid,kind in [(claim,'view_claim_form','form'),(evidence,'view_evidence_form','form'),
            (evidence,'view_evidence_review_list','list'),(evidence,'view_evidence_review_search','search'),
            (self.env['supplier.shortage.warehouse.observation'].with_user(self.user),'view_warehouse_observation_form','form')]:
            self.assertTrue(model.get_view(self.env.ref('supplier_shortage_claim_reconciliation.'+xmlid).id,kind)['arch'])
        self.assertEqual(evidence.action_new_revision()['res_model'],'supplier.shortage.evidence.revision.wizard')

    def test_revision_wizard_stores_actual_binary_content(self):
        claim=self._claim_ready()
        first=self._file(claim)
        wizard=self.env['supplier.shortage.evidence.revision.wizard'].with_user(self.user).create({
            'evidence_id':first.id,'scope_line_ids':[Command.set(claim.claim_line_ids.ids)],
            'upload_data':base64.b64encode(b'Actual wizard revision content').decode(), 'filename':'revision.txt',
            'change_reason':'Corrected warehouse count explanation'})
        action=wizard.action_create_revision()
        revised=first.browse(action['res_id'])
        self.assertEqual(revised.attachment_id.raw.content,b'Actual wizard revision content')
        self.assertEqual(revised.effective_state,'not_reviewed')

    def test_warehouse_correction_workspace_preserves_review(self):
        claim=self._claim_ready()
        previous=claim.warehouse_observation_ids
        checker,stamp=previous.verified_by,previous.verified_at
        action=previous.action_prepare_correction()
        wizard=self.env[action['res_model']].with_user(self.user).browse(action['res_id'])
        wizard.change_reason='New independently attributable physical count explanation'
        result=wizard.action_create_correction()
        current=previous.browse(result['res_id'])
        self.assertEqual(current.supersedes_id,previous)
        self.assertFalse(previous.is_current)
        self.assertEqual(previous.verified_by,checker)
        self.assertEqual(previous.verified_at,stamp)
        self.assertEqual(current.verification_state,'not_reviewed')
        self.assertEqual(current.correction_reason,wizard.change_reason)
        claim.action_evaluate_readiness()
        self.assertNotEqual(claim.readiness_status,'ready')

    def test_hidden_line_scope_hides_evidence_and_document_counts(self):
        claim=self._claim_ready()
        evidence=self._file(claim)
        line=claim.claim_line_ids
        attachment=evidence.attachment_id
        self.env['ir.access'].create({'name':'Evidence scope negative security test',
            'model_id':self.env['ir.model']._get_id('supplier.shortage.claim.line'),
            'operation':'r','domain':"[] if user.id != %s else [('id','!=',%s)]"%(self.user.id,line.id)})
        self.assertFalse(evidence.search([('id','=',evidence.id)]))
        with self.assertRaises(AccessError): evidence.read(['description'])
        with self.assertRaises(AccessError): attachment._to_http_stream()
        self.assertFalse(claim.warehouse_observation_ids.search([('claim_id','=',claim.id)]))

    def test_native_odoo20_binary_upload_values(self):
        from odoo.tools import BinaryBytes
        claim=self._claim_ready()
        for value in (BinaryBytes(b'Native binary count evidence'),
                {'content':base64.b64encode(b'Native binary count evidence').decode(),'filename':'count.txt'}):
            document=self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({
                'claim_id':claim.id,'claim_line_id':claim.claim_line_ids.id,'evidence_type':'count',
                'description':'Native Odoo 20 binary field upload','filename':'count.txt','upload_data':value})
            self.assertEqual(document.attachment_id.raw.content,b'Native binary count evidence')
