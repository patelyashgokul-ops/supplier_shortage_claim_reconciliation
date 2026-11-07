from odoo import Command
from odoo.exceptions import AccessError
from .test_vendor_billing import VendorBillingCase


class TestShortageDetection(VendorBillingCase):
    def _detect(self, order):
        order.with_user(self.user).action_run_shortage_detection()
        return self.env['supplier.shortage.receipt.exception'].search([('reconciliation_kind','=','detection'),('purchase_id','=',order.id)])

    def test_detection_reconciled(self):
        order = self._order(100)
        self._validate(self._incoming(order))
        self._bill([(order.order_line,100)],True)
        rec = self._detect(order)
        self.assertEqual(rec.classification,'reconciled')
        self.assert_qty(rec.audit_event_ids[0].valuation_basis['detection_quantities']['net_received_qty'],100)
        self.assert_qty(rec.candidate_qty,0)

    def test_detection_pending_then_closed_candidate_then_receipt(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._bill([(order.order_line,100)],True)
        rec = self._detect(order)
        self.assertEqual(rec.classification,'pending_supplier_delivery')
        self.assert_qty(rec.potential_billed_excess_qty,15)
        self.assert_qty(rec.candidate_qty,0)
        self._incoming(order).action_cancel()
        self.assertEqual(self._detect(order),rec)
        self.assertEqual(rec.classification,'shortage_candidate_review')
        self.assert_qty(rec.candidate_qty,15)
        self.assertEqual(rec.valuation_status,'indicative')
        self.assertEqual(rec.indicative_untaxed_exposure,150)
        before = rec.source_signature
        history = rec.audit_event_ids
        self._detect(order)
        self.assertEqual(rec.source_signature,before)
        self.assertEqual(rec.audit_event_ids,history)

    def test_detection_draft_excluded_and_overlap(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        self._bill([(order.order_line,85)],True)
        draft = self._bill([(order.order_line,15)])
        rec = self._detect(order)
        self.assert_qty(rec.potential_billed_excess_qty,0)
        self.assertIn('draft_billing_review',rec.findings)
        claim = self._claim(order)
        draft.action_post()
        self._detect(order)
        self.assert_qty(rec.candidate_qty,15)
        self.assertIn(claim,rec.related_claim_ids)
        self.assertEqual(rec.valuation_status,'review')
        self.assertEqual(claim.state,'draft')
        self.assert_qty(claim.claim_line_ids.proposed_qty,1)

    def test_detection_source_fields_cannot_be_forged(self):
        rec = self._detect(self._order(100))
        with self.assertRaises(AccessError):
            rec.with_user(self.user).write({'candidate_qty':500})
        with self.assertRaises(AccessError):
            rec.with_user(self.user).write({'review_state':'resolved'})


    def test_detection_new_receipt_reopens_verified_review(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        self._bill([(order.order_line,100)],True)
        rec = self._detect(order)
        rec.with_user(self.user).write({'review_reason':'Warehouse and invoice evidence reviewed'})
        rec.with_user(self.user).action_verify_candidate()
        self.assertEqual(rec.review_state,'resolved')
        picking = self.env['stock.picking'].create({
            'partner_id':self.supplier.id, 'picking_type_id':self.wh.in_type_id.id,
            'location_id':self.vendor_location.id, 'location_dest_id':self.wh.lot_stock_id.id,
            'move_ids':[Command.create({'product_id':self.product.id,
                'product_uom_qty':15, 'uom_id':order.order_line.uom_id.id,
                'purchase_line_id':order.order_line.id, 'location_id':self.vendor_location.id,
                'location_dest_id':self.wh.lot_stock_id.id})]})
        picking.action_confirm()
        self._validate(picking,15)
        self._detect(order)
        self.assert_qty(rec.candidate_qty,0)
        self.assertEqual(rec.classification,'reconciled')
        self.assertEqual(rec.review_state,'new')
        self.assertTrue(rec.audit_event_ids)

    def test_detection_duplicate_and_credit_block_candidate(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        self._bill([(order.order_line,50)],True,reference='duplicate')
        self._bill([(order.order_line,50)],True,reference='duplicate')
        rec = self._detect(order)
        self.assertEqual(rec.classification,'source_data_conflict')
        self.assertIn('potential_duplicate_bill',rec.findings)
        self.assert_qty(rec.candidate_qty,0)
        self._bill([(order.order_line,10)],True,move_type='in_refund')
        self._detect(order)
        self.assert_qty(rec.posted_billed_qty,100)
        self.assert_qty(rec.posted_credit_qty,10)
        self.assert_qty(rec.candidate_qty,0)

    def test_detection_over_receipt_and_overbilling_batch(self):
        order = self._order(100,products=[self.product,self.second])
        self._validate(self._incoming(order),105)
        self._bill([(order.order_line[0],100),(order.order_line[1],120)],True)
        records = self._detect(order)
        self.assertEqual(len(records),2)
        first = records.filtered(lambda r:r.purchase_line_id == order.order_line[0])
        second = records-first
        self.assertEqual(first.classification,'over_receipt')
        self.assert_qty(first.candidate_qty,0)
        self.env['purchase.order.line'].create({'order_id':order.id,'display_type':'line_section','name':'Delivery Review','product_qty':0})
        order.order_line.with_user(self.user).action_run_shortage_detection()
        self.assertIn('overbilling_above_order',second.findings)
        self.assert_qty(second.potential_billed_excess_qty,15)

    def test_detection_ambiguous_price_has_no_estimate(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        self._bill([(order.order_line,50)],True)
        bill = self._bill([(order.order_line,50)])
        bill.invoice_line_ids.price_unit = 12
        bill.action_post()
        rec = self._detect(order)
        self.assert_qty(rec.candidate_qty,15)
        self.assertEqual(rec.valuation_status,'review')
        self.assertEqual(rec.indicative_untaxed_exposure,0)


    def test_detection_company_isolation_and_unauthorized_decisions(self):
        from odoo.tests.common import new_test_user
        ordinary = new_test_user(self.env, login='ssc_detection_ordinary',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        rec = self._detect(self._order(100))
        rec.with_user(self.user).write({'assigned_user_id':ordinary.id})
        rec.with_user(ordinary).write({'review_reason':'Investigation recorded by ordinary reviewer'})
        with self.assertRaises(AccessError):
            rec.with_user(ordinary).action_dismiss()
        other_wh = self.env['stock.warehouse'].search([('company_id','=',self.other.id)],limit=1)
        if not other_wh:
            other_wh=self.env['stock.warehouse'].create({'name':'SSC Detection Company B','code':'SDB','company_id':self.other.id})
        order = self._order(10,warehouse=other_wh)
        # Do not inherit the administrator's additional companies from with_company.
        order.with_user(self.multi).with_context(allowed_company_ids=self.other.ids).action_run_shortage_detection()
        other_rec = self.env['supplier.shortage.receipt.exception'].search([('reconciliation_kind','=','detection'),('purchase_id','=',order.id)])
        self.assertTrue(other_rec)
        self.assertFalse(other_rec.with_user(ordinary).search([('id','in',other_rec.ids)]))
        with self.assertRaises(AccessError):
            other_rec.with_user(ordinary).read(['candidate_qty'])

    def test_detection_discounted_uom_price_and_source_immutability(self):
        box = self.env['uom.uom'].create({'name':'Detection Box 10', 'relative_uom_id':self.product.uom_id.id,'relative_factor':10})
        order = self._order(10,uom=box)
        order.order_line.discount = 10
        picking = self._incoming(order)
        self._validate(picking,box._compute_quantity(8,picking.move_ids.uom_id,round=False))
        self._incoming(order).action_cancel()
        bill = self._bill([(order.order_line,10)])
        bill.invoice_line_ids.discount = 10
        bill.action_post()
        before = (order.write_date,order.order_line.write_date,order.picking_ids.mapped('write_date'),bill.write_date)
        rec = self._detect(order)
        self.assert_qty(rec.candidate_qty,2,uom=box)
        self.assertEqual(rec.indicative_untaxed_exposure,18)
        self.assertEqual(before,(order.write_date,order.order_line.write_date,order.picking_ids.mapped('write_date'),bill.write_date))

    def test_detection_inaccessible_accounting_is_explicitly_incomplete(self):
        order = self._order(100)
        bill = self._bill([(order.order_line,100)],True)
        self.env['ir.access'].create({'name':'Detection hidden posted bill line',
            'model_id':self.env['ir.model']._get_id('account.move.line'), 'operation':'r',
            'domain':"[] if user.id != %s else [('id','not in',%s)]" % (self.user.id,bill.invoice_line_ids.ids)})
        rec = self._detect(order)
        self.assertEqual(rec.classification,'access_incomplete')
        self.assertFalse(rec.evidence_complete)
        self.assertFalse(rec.source_bill_line_ids)
        self.assert_qty(rec.candidate_qty,0)
        self.assertEqual(rec.indicative_untaxed_exposure,0)

    def test_detection_tax_uncertainty_does_not_fabricate_exposure(self):
        order = self._order(100)
        group = self.env['account.tax.group'].create({'name':'Detection Tax','company_id':self.company.id})
        tax = self.env['account.tax'].create({'name':'Detection Inclusive Tax','company_id':self.company.id,
            'type_tax_use':'purchase','amount_type':'percent','amount':10,'tax_group_id':group.id,'price_include_override':'tax_included'})
        order.order_line.tax_ids = tax
        self._validate(self._incoming(order),85)
        self._incoming(order).action_cancel()
        bill = self._bill([(order.order_line,100)])
        bill.invoice_line_ids.tax_ids = tax
        bill.action_post()
        rec = self._detect(order)
        self.assert_qty(rec.candidate_qty,15)
        self.assertEqual(rec.valuation_status,'review')
        self.assertEqual(rec.indicative_untaxed_exposure,0)

    def test_detection_service_line_is_unsupported(self):
        product = self.env['product.product'].create({'name':'Detection Service','type':'service'})
        order = self._order(100,products=[product])
        self._bill([(order.order_line,100)],True)
        rec = self._detect(order)
        self.assertEqual(rec.classification,'unsupported_product_or_uom')
        self.assertFalse(rec.evidence_complete)
        self.assert_qty(rec.candidate_qty,0)

    def test_detection_hidden_claim_allocations_prevent_partial_overlap_conclusion(self):
        order = self._order(100)
        claim = self._claim(order)
        self.env['ir.access'].create({'name':'Detection private claim allocation',
            'model_id':self.env['ir.model']._get_id('supplier.shortage.claim.line'), 'operation':'r',
            'domain':"[] if user.id != %s else [('id','not in',%s)]" % (self.user.id,claim.claim_line_ids.ids)})
        with self.assertRaises(AccessError), self.cr.savepoint():
            order.with_user(self.user).action_run_shortage_detection()
