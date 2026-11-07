from .preparation_fixtures import verify_document, reject_document, submit_evidence_ready
import base64
from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from .test_vendor_billing import VendorBillingCase


class PreparationCase(VendorBillingCase):
    def _candidates(self, products=None, reviewed=True, order=None):
        order = order or self._order(100,products=products)
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        self._bill([(line,100) for line in order.order_line],True)
        order.with_user(self.user).action_run_shortage_detection()
        candidates = self.env['supplier.shortage.receipt.exception'].with_user(self.user).search([
            ('reconciliation_kind','=','detection'),('purchase_id','=',order.id)])
        if reviewed:
            candidates.write({'review_reason':'Warehouse receipt and vendor invoice evidence investigated'})
            candidates.action_verify_candidate()
        return candidates

    def _prepare(self, candidates, quantity=None):
        result = candidates.action_prepare_shortage_claim(None if quantity is None else {str(candidates.id):quantity})
        return self.env['supplier.shortage.claim'].with_user(self.user).browse(result['res_id'])

    def _evidence(self, claim, upload=False, line=None):
        values = {'claim_id':claim.id,'claim_line_id':line.id if line else False,
            'evidence_type':'count' if upload else 'purchase','description':'Warehouse shortage evidence',
            'scope_line_ids':[Command.set((line or claim._review_lines()).ids)]}
        if upload:
            values.update(upload_data=base64.b64encode(b'Actual inventory count: 85 of 100'),filename='count.txt')
        else:
            values['source_purchase_id']=claim.purchase_id.id
        return self.env['supplier.shortage.claim.evidence'].with_user(self.user).create(values)

    def _verified_evidence(self, claim):
        evidence = self._evidence(claim)
        evidence.write({'verification_note':'Reviewed relevant purchase and receiving discrepancy'})
        verify_document(evidence)
        return evidence



class TestClaimPreparation(PreparationCase):
    def test_reviewed_candidate_partial_claims_and_no_source_mutation(self):
        candidate = self._candidates()
        order = candidate.purchase_id
        before = (order.write_date,order.order_line.write_date,order.picking_ids.mapped('write_date'),order.order_line.invoice_lines.move_id.mapped('write_date'))
        source_counts = {model:self.env[model].search_count([]) for model in ('account.move','account.move.line','stock.move','stock.quant','account.payment')}
        quant_position = self.env['stock.quant'].search([('product_id','=',self.product.id)]).read(['quantity','reserved_quantity','location_id'])
        first = self._prepare(candidate,10)
        self.assertEqual(first.state,'draft')
        self.assertEqual(first.partner_id,candidate.partner_id)
        self.assertEqual(first.purchase_id,order)
        self.assertEqual(first.company_id,candidate.company_id)
        self.assert_qty(candidate.allocated_qty,10)
        self.assert_qty(candidate.available_qty,5)
        self.assertEqual(first.claim_line_ids.detection_id,candidate)
        with self.assertRaises(ValidationError),self.cr.savepoint():
            self._prepare(candidate,8)
        second = self._prepare(candidate,5)
        self.assertNotEqual(first.name,second.name)
        self.assertEqual(source_counts,{model:self.env[model].search_count([]) for model in source_counts})
        self.assertEqual(quant_position,self.env['stock.quant'].search([('product_id','=',self.product.id)]).read(['quantity','reserved_quantity','location_id']))
        self.assert_qty(candidate.available_qty,0)
        with self.assertRaises(ValidationError),self.cr.savepoint():
            self._prepare(candidate)
        self.assertEqual(before,(order.write_date,order.order_line.write_date,order.picking_ids.mapped('write_date'),order.order_line.invoice_lines.move_id.mapped('write_date')))

    def test_unreviewed_cleared_and_stale_candidates_block(self):
        candidate = self._candidates(reviewed=False)
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._prepare(candidate)
        candidate.write({'review_reason':'Need investigation, not supplier acceptance'})
        candidate.action_dismiss()
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._prepare(candidate)
        candidate.write({'review_reason':'New warehouse evidence supports reopening'})
        candidate.action_reopen()
        candidate.action_verify_candidate()
        candidate.purchase_line_id.price_unit=11
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._prepare(candidate)

    def test_multi_line_same_product_distinct_sources_and_atomic_failure(self):
        candidates = self._candidates(products=[self.product,self.product])
        claim = self._prepare(candidates)
        self.assertEqual(len(claim.claim_line_ids),2)
        self.assertEqual(set(claim.claim_line_ids.purchase_line_id.ids),set(candidates.purchase_line_id.ids))
        other = self._candidates(products=[self.second])
        before = self.env['supplier.shortage.claim'].search_count([])
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._prepare(candidates|other)
        self.assertEqual(self.env['supplier.shortage.claim'].search_count([]),before)

    def test_direct_create_o2m_copy_and_zero_cannot_bypass(self):
        candidate = self._candidates()
        claim = self._prepare(candidate,10)
        Line = self.env['supplier.shortage.claim.line'].with_user(self.user)
        for quantity in (0,-1,8):
            with self.assertRaises(ValidationError),self.cr.savepoint():
                Line.create({'claim_id':claim.id,'detection_id':candidate.id,'purchase_line_id':candidate.purchase_line_id.id,'proposed_qty':quantity})
        with self.assertRaises(ValidationError),self.cr.savepoint():
            claim.write({'claim_line_ids':[Command.create({'detection_id':candidate.id,'purchase_line_id':candidate.purchase_line_id.id,'proposed_qty':8})]})
        copied = claim.copy()
        self.assertFalse(copied.claim_line_ids.detection_id)
        with self.assertRaises(ValidationError),self.cr.savepoint(): copied.action_submit_for_review()
        copied_line = claim.claim_line_ids.copy()
        self.assertFalse(copied_line.detection_id)
        for values in ({'allocated_qty':1},{'allocation_state':'released'},{'preparation_snapshot':{}}):
            with self.assertRaises(AccessError): claim.claim_line_ids.write(values)

    def test_release_history_and_controlled_correction(self):
        candidate = self._candidates()
        claim = self._prepare(candidate,10)
        line = claim.claim_line_ids
        snapshot = dict(line.preparation_snapshot)
        claim.correction_reason='Corrected counted shortage allocation'
        line.proposed_qty=8
        self.assert_qty(candidate.available_qty,7)
        self.assertEqual(line.preparation_snapshot,snapshot)
        line.action_release_allocation()
        self.assertEqual(line.allocation_state,'released')
        self.assertEqual(line.preparation_snapshot,snapshot)
        self.assert_qty(candidate.available_qty,15)
        self.assertTrue(claim.audit_event_ids.filtered(lambda e:e.event_type=='allocation_released'))

    def test_evidence_upload_source_link_verification_and_submission(self):
        claim = self._prepare(self._candidates())
        claim.shortage_reason='Billed goods missing from warehouse count'
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        evidence = self._evidence(claim,upload=True,line=claim.claim_line_ids)
        self.assertEqual(evidence.company_id,claim.company_id)
        self.assertEqual(evidence.attachment_id.res_model,evidence._name)
        self.assertEqual(evidence.attachment_id.res_id,evidence.id)
        evidence.write({'verification_note':'Count document reviewed against receipt and bill'})
        verify_document(evidence)
        submit_evidence_ready(claim)
        self.assertEqual(claim.state,'under_review')
        with self.assertRaises(ValidationError): claim.claim_line_ids.proposed_qty=1
        with self.assertRaises(ValidationError): evidence.description='Unreviewed replacement'
        claim.action_return_to_draft()
        self.assertEqual(claim.state,'draft')

    def test_evidence_source_attachment_and_verifier_forgery_denied(self):
        claim = self._prepare(self._candidates())
        evidence = self._evidence(claim,upload=True)
        private = self.env['ir.attachment'].create({'name':'private.txt','raw':b'Private'})
        Evidence = evidence.with_user(self.user)
        for vals in ({'attachment_id':private.id},{'verified_by':self.user.id},{'verification_state':'verified'},{'verified_at':'2026-11-03 00:00:00'}):
            with self.assertRaises(AccessError): Evidence.write(vals)
        with self.assertRaises(AccessError): private.with_user(self.user).write({'res_model':evidence._name,'res_id':evidence.id})
        with self.assertRaises(AccessError): evidence.attachment_id.write({'raw':b'Forged'})
        other_order=self._order(100)
        with self.assertRaises(ValidationError),self.cr.savepoint():
            evidence.create({'claim_id':claim.id,'description':'Forged source','evidence_type':'purchase','source_purchase_id':other_order.id})

    def test_bad_file_rolls_back_without_orphan_attachment(self):
        claim=self._prepare(self._candidates())
        attachments=self.env['ir.attachment'].search_count([])
        evidence_count=self.env['supplier.shortage.claim.evidence'].search_count([])
        with self.assertRaises(ValidationError),self.cr.savepoint():
            self.env['supplier.shortage.claim.evidence'].with_user(self.user).create({'claim_id':claim.id,'evidence_type':'photo',
                'description':'Invalid executable file','upload_data':base64.b64encode(b'bad'),'filename':'wrong.exe'})
        self.assertEqual(self.env['ir.attachment'].search_count([]),attachments)
        self.assertEqual(self.env['supplier.shortage.claim.evidence'].search_count([]),evidence_count)

    def test_manual_draft_cannot_submit_unlimited_quantity(self):
        order=self._order(100)
        claim=self._claim(order)
        claim.manual_exception_reason='Historical receiving records need investigation'
        claim.shortage_reason='Physical evidence is not yet complete'
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        self.assertEqual(claim.state,'draft')
        self.assertFalse(claim.audit_event_ids.filtered(lambda e:e.event_type=='submitted'))

    def test_nonmanager_cannot_verify_evidence_or_release(self):
        ordinary = new_test_user(self.env,login='ssc_preparation_user',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
            company_id=self.company.id,company_ids=[Command.set(self.company.ids)])
        claim=self._prepare(self._candidates(),10)
        evidence=self._evidence(claim)
        evidence.with_user(ordinary).write({'verification_note':'User requests manager review of evidence'})
        with self.assertRaises(AccessError): evidence.with_user(ordinary).action_verify()
        with self.assertRaises(AccessError): claim.claim_line_ids.with_user(ordinary).action_release_allocation()

    def test_reduced_source_requires_revalidation_preserving_snapshot(self):
        candidate=self._candidates()
        claim=self._prepare(candidate,10)
        snapshot=dict(claim.claim_line_ids.preparation_snapshot)
        claim.shortage_reason='Warehouse discrepancy requires review'
        self._verified_evidence(claim)
        candidate.purchase_line_id.product_qty=90
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        candidate.purchase_line_id.with_user(self.user).action_run_shortage_detection()
        self.assertTrue(candidate.allocation_integrity_blocked)
        self.assertEqual(claim.claim_line_ids.preparation_snapshot,snapshot)
        self.assertEqual(claim.state,'draft')

    def test_claim_and_detection_views_load(self):
        candidate=self._candidates()
        claim=self._prepare(candidate,5)
        for model,xmlid,kind in [(claim,'view_claim_form','form'),(candidate,'view_detection_form','form'),
                (self.env['supplier.shortage.claim.evidence'].with_user(self.user),'view_evidence_form','form')]:
            self.assertTrue(model.get_view(self.env.ref('supplier_shortage_claim_reconciliation.'+xmlid).id,kind)['arch'])
        self.assertIn('action_prepare_claim_dialog',candidate.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_detection_form').id,'form')['arch'])
        self.assertEqual(candidate.action_view_allocated_claims()['domain'],[('id','in',claim.ids)])

    def test_uom_partial_allocation_and_manual_binding(self):
        candidate=self._candidates()
        claim=self._prepare(candidate,10)
        box=self.env['uom.uom'].create({'name':'Preparation Box 10','relative_uom_id':candidate.product_uom_id.id,'relative_factor':10})
        Line=self.env['supplier.shortage.claim.line'].with_user(self.user)
        with self.assertRaises(ValidationError),self.cr.savepoint():
            Line.create({'claim_id':claim.id,'purchase_line_id':candidate.purchase_line_id.id,'detection_id':candidate.id,
                'proposed_qty':0.8,'claim_uom_id':box.id})
        partial=Line.create({'claim_id':claim.id,'purchase_line_id':candidate.purchase_line_id.id,
            'proposed_qty':0.5,'claim_uom_id':box.id})
        claim.manual_exception_reason='Manual count entered in configured purchase packaging'
        partial.action_bind_reviewed_candidate(candidate.id)
        self.assert_qty(partial.allocated_qty,5)
        self.assert_qty(candidate.available_qty,0)
        candidate.purchase_line_id.with_user(self.user).action_run_shortage_detection()
        self.assertEqual(candidate.review_state,'resolved')
        self.assert_qty(candidate.allocated_qty,15)

    def test_company_isolation_for_conversion_and_evidence_download(self):
        local=self._candidates()
        other_wh=self.env['stock.warehouse'].search([('company_id','=',self.other.id)],limit=1)
        order=self._order(100,warehouse=other_wh)
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        self._bill([(order.order_line,100)],True)
        self.multi.write({'group_ids':[Command.link(self.env.ref('supplier_shortage_claim_reconciliation.group_claim_manager').id)]})
        order.with_user(self.multi).action_run_shortage_detection()
        foreign=self.env['supplier.shortage.receipt.exception'].with_user(self.multi).search([('reconciliation_kind','=','detection'),('purchase_id','=',order.id)])
        foreign.write({'review_reason':'Company B receiving and posted bill evidence reviewed'})
        foreign.action_verify_candidate()
        with self.assertRaises(ValidationError),self.cr.savepoint():
            (local|foreign).with_user(self.multi).action_prepare_shortage_claim()
        with self.assertRaises(AccessError),self.cr.savepoint(): foreign.with_user(self.user).action_prepare_shortage_claim()
        action=foreign.action_prepare_shortage_claim()
        evidence=self.env['supplier.shortage.claim.evidence'].with_user(self.multi).create({'claim_id':action['res_id'],
            'evidence_type':'count','description':'Private Company B count','filename':'count.txt','upload_data':base64.b64encode(b'Company B shortage count')})
        with self.assertRaises(AccessError): evidence.attachment_id.with_user(self.user)._to_http_stream()
        with self.assertRaises(AccessError): evidence.with_user(self.user).read(['description'])

    def test_hidden_allocations_and_source_bill_do_not_become_zero_availability(self):
        candidate=self._candidates()
        claim=self._prepare(candidate,10)
        self.env['ir.access'].create({'name':'Restricted claim allocation evidence','model_id':self.env['ir.model']._get_id('supplier.shortage.claim.line'),
            'operation':'r','domain':"[] if user.id != %s else [('id','not in',%s)]"%(self.user.id,claim.claim_line_ids.ids)})
        with self.assertRaises(AccessError): candidate.invalidate_recordset(); candidate.read(['available_qty'])
        with self.assertRaises(AccessError),self.cr.savepoint(): self._prepare(candidate,5)

    def test_rejected_evidence_is_not_sufficient_and_history_retained(self):
        claim=self._prepare(self._candidates())
        claim.shortage_reason='Shortage requires relevant supporting documents'
        evidence=self._evidence(claim,upload=True)
        evidence.verification_note='Document does not evidence this delivery discrepancy'
        reject_document(evidence)
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        self.assertEqual(evidence.verification_state,'rejected')
        self.assertTrue(evidence.attachment_id)
        with self.assertRaises(AccessError): evidence.unlink()
        with self.assertRaises(AccessError): claim.claim_line_ids.unlink()

    def test_source_review_renewal_preserves_original_preparation(self):
        candidate=self._candidates()
        claim=self._prepare(candidate,10)
        original=dict(claim.claim_line_ids.preparation_snapshot)
        candidate.purchase_line_id.price_unit=11
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        candidate.purchase_line_id.with_user(self.user).action_run_shortage_detection()
        candidate.write({'review_reason':'Updated purchase pricing source investigated; quantity eligibility retained'})
        candidate.action_verify_candidate()
        claim.correction_reason='Revalidate against newly reviewed purchase pricing'
        claim.action_revalidate_preparation()
        self.assertEqual(claim.claim_line_ids.preparation_snapshot,original)
        self.assertNotEqual(claim.claim_line_ids.allocation_basis['source_signature'],original['source_signature'])
        claim.shortage_reason='Billed items absent from completed warehouse count'
        self._verified_evidence(claim)
        submit_evidence_ready(claim)
        self.assertEqual(claim.state,'under_review')

    def test_changed_purchase_uom_cannot_reinterpret_existing_reservations(self):
        candidate=self._candidates()
        self._prepare(candidate,10)
        box=self.env['uom.uom'].create({'name':'Changed Purchase Box 10','relative_uom_id':candidate.product_uom_id.id,'relative_factor':10})
        candidate.purchase_line_id.write({'uom_id':box.id,'product_qty':10,'price_unit':100})
        # Odoo 20 does not invalidate stored qty_received on this UoM correction.
        # Recompute it natively in the source fixture; the engine must not repair sources.
        candidate.purchase_line_id.with_user(self.env.user)._compute_qty_received()
        candidate.purchase_id.picking_ids.filtered(lambda p:p.state not in ('done','cancel')).action_cancel()
        candidate.purchase_line_id.with_user(self.user).action_run_shortage_detection()
        self.assertFalse(candidate.allocation_quantity_known)
        self.assertTrue(candidate.allocation_integrity_blocked)
        candidate.write({'review_reason':'Corrected purchase UoM verified; existing reservations require authorized correction'})
        candidate.action_verify_candidate()
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._prepare(candidate,1)

    def test_current_critical_source_exception_blocks_preparation(self):
        candidate=self._candidates()
        Exception=self.env['supplier.shortage.receipt.exception'].with_user(self.user)
        scope=[('source_key','=','step10-critical-source-%s'%candidate.id)]
        finding={'source_key':'step10-critical-source-%s'%candidate.id,'company_id':candidate.company_id.id,
            'purchase_id':candidate.purchase_id.id,'purchase_line_id':candidate.purchase_line_id.id,
            'exception_type':'other_review','reconciliation_kind':'receiving','severity':'high',
            'quantity_known':False,'product_uom_id':candidate.product_uom_id.id,'source_signature':'critical-source-review',
            'description':'Warehouse source integrity requires explicit review before allocation',
            'source_move_ids':[Command.set(candidate.source_move_ids.ids)]}
        critical=Exception._synchronize(scope,{finding['source_key']:{k:v for k,v in finding.items() if k!='source_key'}})
        with self.assertRaises(ValidationError),self.cr.savepoint(): self._prepare(candidate,5)
        critical.write({'review_reason':'Warehouse evidence conflict investigated and resolved'})
        critical.action_resolve()
        self.assertEqual(self._prepare(candidate,5).state,'draft')

    def test_warehouse_contribution_is_limited_to_receiving_evidence(self):
        claim=self._prepare(self._candidates(),5)
        warehouse=new_test_user(self.env,login='ssc_evidence_warehouse',groups='supplier_shortage_claim_reconciliation.group_warehouse_user,stock.group_stock_user,purchase.group_purchase_user',
            company_id=self.company.id,company_ids=[Command.set(self.company.ids)])
        evidence=self.env['supplier.shortage.claim.evidence'].with_user(warehouse).create({'claim_id':claim.id,
            'evidence_type':'count','description':'Warehouse physical count','filename':'count.txt','upload_data':base64.b64encode(b'Count checked at receiving')})
        self.assertEqual(evidence.create_uid,warehouse)
        self.assertEqual(evidence.attachment_id.create_uid,warehouse)
        with self.assertRaises(AccessError): evidence.action_verify()
        with self.assertRaises(AccessError),self.cr.savepoint():
            evidence.create({'claim_id':claim.id,'evidence_type':'supplier','description':'Financial settlement letter',
                'filename':'letter.txt','upload_data':base64.b64encode(b'Unverified financial letter')})

    def test_configuration_role_can_update_only_authorized_evidence_policy(self):
        admin=new_test_user(self.env,login='ssc_preparation_config_admin',groups='supplier_shortage_claim_reconciliation.group_module_admin',
            company_id=self.company.id,company_ids=[Command.set(self.company.ids)])
        Wizard=self.env['supplier.shortage.evidence.policy.wizard'].with_user(admin)
        wizard=Wizard.create({'company_id':self.company.id,'minimum_verified':2,'maximum_mb':5,'extensions':'pdf,txt'})
        wizard.action_apply()
        self.assertEqual(self.company.ssc_min_verified_evidence,2)
        self.assertEqual(self.company.ssc_evidence_max_mb,5)
        claim=self._prepare(self._candidates(),5)
        claim.shortage_reason='Policy requires two internally verified supporting records'
        self._verified_evidence(claim)
        self.assertEqual(claim.preparation_status,'incomplete')
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        additional=self._evidence(claim,upload=True)
        additional.verification_note='Count document provides additional physical evidence'
        verify_document(additional)
        self.assertEqual(claim.preparation_status,'ready')
        submit_evidence_ready(claim)
        self.assertEqual(claim.state,'under_review')
        self.assertFalse(admin.has_group('supplier_shortage_claim_reconciliation.group_claim_user'))
        with self.assertRaises(AccessError):
            self.env['supplier.shortage.claim'].with_user(admin).create({'partner_id':self.supplier.id})
        foreign=Wizard.create({'company_id':self.other.id,'minimum_verified':1,'maximum_mb':5,'extensions':'txt'})
        with self.assertRaises(AccessError): foreign.action_apply()
        wizard.minimum_verified=0
        with self.assertRaises(ValidationError): wizard.action_apply()

    def test_tax_basis_change_requires_fresh_source_review(self):
        order=self._order(100)
        group=self.env['account.tax.group'].create({'name':'Preparation Tax Basis','company_id':self.company.id})
        tax=self.env['account.tax'].create({'name':'Inclusive Source Tax','company_id':self.company.id,'type_tax_use':'purchase',
            'amount_type':'percent','amount':10,'tax_group_id':group.id,'price_include_override':'tax_included'})
        order.order_line.tax_ids=tax
        claim=self._prepare(self._candidates(order=order),5)
        original=dict(claim.claim_line_ids.preparation_snapshot)
        claim.shortage_reason='Confirmed physical discrepancy with provisional tax valuation'
        self._verified_evidence(claim)
        tax.amount=12
        with self.assertRaises(ValidationError),self.cr.savepoint(): claim.action_submit_for_review()
        self.assertEqual(claim.claim_line_ids.preparation_snapshot,original)
        self.assertEqual(claim.state,'draft')
