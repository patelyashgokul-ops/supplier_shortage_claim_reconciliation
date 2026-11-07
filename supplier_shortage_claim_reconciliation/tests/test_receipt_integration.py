from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from copy import deepcopy

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user


class ReceiptIntegrationCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other = cls.env['res.company'].create({'name': 'SSC Receipt Company B'})
        cls.other_wh = cls.env['stock.warehouse'].search([('company_id','=',cls.other.id)],limit=1) or cls.env['stock.warehouse'].create({
            'name':'SSC Company B Warehouse','code':'SCB','company_id':cls.other.id})
        cls.wh = cls.env['stock.warehouse'].search([('company_id','=',cls.company.id)],limit=1)
        cls.wh.write({'reception_steps':'one_step'})
        cls.vendor_location = cls.env.ref('stock.stock_location_suppliers')
        cls.customer_location = cls.env.ref('stock.stock_location_customers')
        cls.supplier = cls.env['res.partner'].create({'name':'SSC Receipt Supplier'})
        cls.product = cls.env['product.product'].create({'name':'SSC Receipt Goods','type':'consu','is_storable':True})
        cls.untracked = cls.env['product.product'].create({'name':'SSC Untracked Goods','type':'consu','is_storable':False})
        cls.second = cls.env['product.product'].create({'name':'SSC Other Goods','type':'consu','is_storable':True})
        cls.user = new_test_user(cls.env,login='ssc_receipt_user',
            groups='supplier_shortage_claim_reconciliation.group_claim_manager,purchase.group_purchase_user,stock.group_stock_manager',
            company_id=cls.company.id,company_ids=[Command.set([cls.company.id])])
        cls.no_stock = new_test_user(cls.env,login='ssc_receipt_no_stock',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user',
            company_id=cls.company.id,company_ids=[Command.set([cls.company.id])])
        cls.multi = new_test_user(cls.env,login='ssc_receipt_multi',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,stock.group_stock_manager',
            company_id=cls.company.id,company_ids=[Command.set([cls.company.id,cls.other.id])])

    def _order(self, quantity=200, products=None, warehouse=None, uom=None):
        warehouse = warehouse or self.wh
        products = products or [self.product]
        order = self.env['purchase.order'].with_context(allowed_company_ids=warehouse.company_id.ids).with_company(warehouse.company_id).create({
            'partner_id':self.supplier.id,'picking_type_id':warehouse.in_type_id.id,'user_id':False,
            'order_line':[Command.create({'product_id':product.id,'product_qty':quantity,
                'uom_id':(uom or product.uom_id).id,'price_unit':10,'tax_ids':[Command.clear()]}) for product in products]})
        order.button_confirm()
        self.assertEqual(order.state,'purchase')
        return order

    def _claim(self, order, user=None):
        return self.env['supplier.shortage.claim'].with_user(user or self.user).create({
            'purchase_id':order.id,'claim_line_ids':[Command.create({'purchase_line_id':line.id,'proposed_qty':1}) for line in order.order_line]})

    def _incoming(self, order):
        return order.picking_ids.filtered(lambda picking: picking.location_id.usage == 'supplier' and picking.state not in ('done','cancel'))[:1]

    def _validate(self, picking, quantity=None):
        picking.action_assign()
        for move in picking.move_ids.filtered(lambda record: record.state not in ('done','cancel')):
            move.quantity = move.product_uom_qty if quantity is None else quantity
            move.picked = True
        result = picking.with_context(skip_backorder=True).button_validate()
        self.assertTrue(result is True or isinstance(result,dict))
        self.assertEqual(picking.state,'done')
        return picking

    def _refresh(self, claim):
        claim.claim_line_ids.invalidate_recordset()
        claim.invalidate_recordset()
        return claim.claim_line_ids

    def assert_qty(self, value, expected, uom=None):
        self.assertEqual((uom or self.product.uom_id).compare(value,expected),0)

    def _extra_movement(self, order, source, destination, original=None, warehouse=None, link=True):
        warehouse = warehouse or self.wh
        picking = self.env['stock.picking'].with_company(warehouse.company_id).create({
            'partner_id':self.supplier.id,'picking_type_id':warehouse.int_type_id.id,
            'location_id':source.id,'location_dest_id':destination.id})
        move = self.env['stock.move'].create({'product_id':self.product.id,'product_uom_qty':10,
            'uom_id':self.product.uom_id.id,'picking_id':picking.id,'location_id':source.id,'location_dest_id':destination.id,
            'purchase_line_id':order.order_line[:1].id if link else False,
            'origin_returned_move_id':original.id if original else False})
        picking.action_confirm()
        return picking,move


class TestReceiptIntegration(ReceiptIntegrationCase):
    def test_one_validated_receipt_and_actual_move_traceability(self):
        order = self._order(100)
        picking = self._validate(self._incoming(order))
        claim = self._claim(order)
        line = claim.claim_line_ids
        self.assertTrue(line.receiving_complete)
        self.assertEqual(line.receiving_status,'full')
        self.assert_qty(line.receipt_gross_qty,100)
        self.assert_qty(line.receipt_net_qty,100)
        self.assertEqual(claim.receipt_count,1)
        self.assertEqual(set(line.receiving_basis['move_ids']),set(picking.move_ids.ids))
        self.assertEqual(picking.move_ids.ssc_receipt_kind,'receipt')
        self.assert_qty(picking.move_ids.ssc_completed_purchase_qty,100)

    def test_three_partial_receipts_180_of_200_with_open_20_backorder(self):
        order = self._order()
        completed = self.env['stock.picking']
        for quantity in (80,60,40):
            completed |= self._validate(self._incoming(order),quantity)
        claim = self._claim(order)
        line = claim.claim_line_ids
        self.assert_qty(line.receipt_gross_qty,180)
        self.assert_qty(line.receipt_unreceived_qty,20)
        self.assert_qty(line.receipt_backorder_qty,20)
        self.assertEqual(line.receiving_status,'partial')
        self.assertEqual(len(completed),3)
        self.assertEqual(claim.receipt_count,4)
        self.assert_qty(line.proposed_qty,1)
        self.assertEqual(claim.state,'draft')
        self.assertNotIn('verified_shortage_qty',line._fields)

    def test_unvalidated_reserved_quantity_and_cancellation_never_count_as_done(self):
        order = self._order(100)
        picking = self._incoming(order)
        picking.move_ids.quantity = 70
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,0)
        self.assert_qty(claim.claim_line_ids.receipt_pending_qty,100)
        self.assertEqual(claim.claim_line_ids.receiving_status,'pending')
        picking.action_cancel()
        line = self._refresh(claim)
        self.assert_qty(line.receipt_gross_qty,0)
        self.assertEqual(line.receiving_status,'no_receipt')
        self.assertIn('Cancelled',line.receiving_note)
        self.assertEqual(claim.state,'draft')

    def test_backorder_completion_and_cancellation_are_live_information(self):
        order = self._order(100)
        self._validate(self._incoming(order),70)
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.receipt_backorder_qty,30)
        backorder = self._incoming(order)
        self.assertTrue(backorder.backorder_id)
        self._validate(backorder)
        line = self._refresh(claim)
        self.assertEqual(line.receiving_status,'full')
        self.assert_qty(line.receipt_gross_qty,100)
        self.assert_qty(line.receipt_backorder_qty,0)
        second = self._order(100)
        self._validate(self._incoming(second),70)
        other_claim = self._claim(second)
        self._incoming(second).action_cancel()
        other_line = self._refresh(other_claim)
        self.assertEqual(other_line.receiving_status,'partial')
        self.assert_qty(other_line.receipt_unreceived_qty,30)
        self.assert_qty(other_line.receipt_backorder_qty,0)
        self.assertEqual(other_claim.state,'draft')

    def _multi_step(self, steps, expected_internal):
        self.wh.reception_steps = steps
        order = self._order(100)
        self._validate(self._incoming(order),80)
        internal = order.picking_ids.filtered(lambda picking: picking.location_id.usage == 'internal' and picking.state not in ('done','cancel'))
        # Native routes may keep internal moves outside order.picking_ids; follow the real chain.
        roots = order.order_line.move_ids
        downstream = roots.move_dest_ids
        internal |= downstream.picking_id.filtered(lambda picking: picking.location_id.usage == 'internal' and picking.state not in ('done','cancel'))
        finished = self.env['stock.picking']
        while internal:
            next_picking = internal.filtered(lambda picking: picking.state in ('assigned','confirmed'))[:1]
            if not next_picking:
                break
            self._validate(next_picking,80)
            finished |= next_picking
            downstream |= downstream.move_dest_ids
            internal = downstream.picking_id.filtered(lambda picking: picking.location_id.usage == 'internal' and picking.state not in ('done','cancel')) - finished
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,80)
        self.assertEqual(claim.claim_line_ids.receiving_status,'partial')
        self.assertGreaterEqual(len(finished),expected_internal)
        self.assertTrue(set(finished.move_ids.ids) & set(claim.claim_line_ids.receiving_basis['move_ids']))
        return order,claim

    def test_two_step_receiving_does_not_double_count(self):
        self._multi_step('two_steps',1)

    def test_three_step_receiving_does_not_triple_count(self):
        self._multi_step('three_steps',2)

    def test_interwarehouse_receipt_like_operation_is_not_supplier_delivery(self):
        order = self._order(100)
        second_wh = self.env['stock.warehouse'].create({'name':'SSC Other Warehouse','code':'SSCW2','company_id':self.company.id})
        picking,move = self._extra_movement(order,self.wh.lot_stock_id,second_wh.lot_stock_id)
        picking.picking_type_id = second_wh.in_type_id
        picking.write({'location_id':self.wh.lot_stock_id.id,'location_dest_id':second_wh.lot_stock_id.id})
        move.write({'location_id':self.wh.lot_stock_id.id,'location_dest_id':second_wh.lot_stock_id.id})
        self._validate(picking,10)
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,0)
        self.assertNotEqual(claim.claim_line_ids.receiving_status,'full')
        self.assertEqual(move.ssc_receipt_kind,'internal')

    def test_linked_partial_supplier_returns_and_net_no_double_subtraction(self):
        order = self._order(100)
        receipt = self._validate(self._incoming(order))
        return_picking = receipt._create_return()
        return_picking.move_ids.to_refund = True
        self._validate(return_picking,10)
        claim = self._claim(order)
        line = claim.claim_line_ids
        self.assert_qty(line.receipt_gross_qty,100)
        self.assert_qty(line.receipt_return_qty,10)
        self.assert_qty(line.receipt_net_qty,90)
        self.assert_qty(line.receipt_unreceived_qty,0)
        self.assertEqual(line.receiving_status,'returns')
        self.assert_qty(order.order_line.qty_received,90)
        self.assertEqual(return_picking.move_ids.ssc_receipt_kind,'return')

    def test_multiple_returns_and_later_replacement_keep_move_identities(self):
        order = self._order(100)
        receipt = self._validate(self._incoming(order))
        first = receipt._create_return()
        self._validate(first,10)
        second = receipt._create_return()
        self._validate(second,5)
        replacement = first._create_return()
        self._validate(replacement,10)
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,110)
        self.assert_qty(claim.claim_line_ids.receipt_return_qty,15)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,95)
        self.assertEqual(replacement.move_ids.ssc_receipt_kind,'replacement')
        self.assertEqual(len({*receipt.move_ids.ids,*first.move_ids.ids,*second.move_ids.ids,*replacement.move_ids.ids}),4)

    def test_return_without_origin_requires_review_not_unsupported_adjustment(self):
        order = self._order(100)
        self._validate(self._incoming(order))
        picking,move = self._extra_movement(order,self.wh.lot_stock_id,self.vendor_location)
        self._validate(picking,10)
        claim = self._claim(order)
        self.assertFalse(claim.claim_line_ids.receiving_complete)
        self.assertEqual(claim.claim_line_ids.receiving_status,'requires_review')
        self.assertEqual(move.ssc_receipt_kind,'review')

    def test_customer_delivery_and_inventory_loss_are_not_supplier_returns(self):
        order = self._order(100)
        self._validate(self._incoming(order))
        delivery,move = self._extra_movement(order,self.wh.lot_stock_id,self.customer_location)
        self._validate(delivery,10)
        loss = self.env['stock.location'].create({'name':'SSC Inventory Loss','usage':'inventory','company_id':self.company.id})
        scrap,other_move = self._extra_movement(order,self.wh.lot_stock_id,loss)
        self._validate(scrap,5)
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,100)
        self.assert_qty(claim.claim_line_ids.receipt_return_qty,0)
        self.assertEqual(move.ssc_receipt_kind,'excluded')
        self.assertEqual(other_move.ssc_receipt_kind,'excluded')

    def test_products_and_duplicate_product_po_lines_are_distinct(self):
        order = self._order(10,products=[self.product,self.second,self.product])
        receipt = self._incoming(order)
        for index,move in enumerate(receipt.move_ids.sorted('purchase_line_id')):
            move.quantity = index + 1
            move.picked = True
        receipt.with_context(skip_backorder=True).button_validate()
        claim = self._claim(order)
        self.assertEqual(len(claim.claim_line_ids),3)
        for line in claim.claim_line_ids:
            sources = receipt.move_ids.filtered(lambda move: move.purchase_line_id == line.purchase_line_id)
            self.assert_qty(line.receipt_gross_qty,sum(sources.mapped('quantity')))
            self.assertEqual({self.env['stock.move'].browse(identifier).purchase_line_id.id for identifier in line.receiving_basis['move_ids']}, {line.purchase_line_id.id})

    def test_other_po_moves_are_excluded_and_counts_deduplicate_same_po_line(self):
        order = self._order(10)
        other = self._order(10)
        self._validate(self._incoming(order),5)
        self._validate(self._incoming(other))
        claim = self._claim(order)
        self.env['supplier.shortage.claim.line'].with_user(self.user).create({'claim_id':claim.id,
            'purchase_line_id':order.order_line.id,'proposed_qty':1})
        self.assertEqual(claim.receipt_count,2)
        for line in claim.claim_line_ids:
            self.assert_qty(line.receipt_gross_qty,5)
            self.assertFalse(set(other.order_line.move_ids.ids) & set(line.receiving_basis['move_ids']))

    def test_configured_uom_108_units_are_9_of_10_boxes(self):
        units = self.product.uom_id
        box = self.env['uom.uom'].create({'name':'SSC Receipt Box 12','relative_uom_id':units.id,'relative_factor':12})
        order = self._order(10,uom=box)
        receipt = self._incoming(order)
        receipt.move_ids.write({'uom_id':units.id,'product_uom_qty':120})
        self._validate(receipt,108)
        line = self._claim(order).claim_line_ids
        self.assert_qty(line.receipt_gross_qty,9,box)
        self.assert_qty(line.receipt_unreceived_qty,1,box)
        self.assertEqual(line.receipt_uom_id,box)

    def test_fractional_and_over_receipts_are_informational(self):
        order = self._order(1.5)
        self._validate(self._incoming(order),0.25)
        line = self._claim(order).claim_line_ids
        self.assert_qty(line.receipt_gross_qty,0.25)
        self.assert_qty(line.receipt_unreceived_qty,1.25)
        over = self._order(100)
        self._validate(self._incoming(over),105)
        claim = self._claim(over)
        self.assertEqual(claim.claim_line_ids.receiving_status,'over')
        self.assert_qty(claim.claim_line_ids.receipt_unreceived_qty,0)
        self.assertEqual(claim.state,'draft')

    def test_lot_and_serial_receipts_trace_native_move_lines_without_double_count(self):
        for tracking in ('lot','serial'):
            product = self.env['product.product'].create({'name':'SSC Tracked '+tracking,'type':'consu','is_storable':True,'tracking':tracking})
            order = self._order(2,products=[product])
            picking = self._incoming(order)
            move = picking.move_ids
            move.move_line_ids.unlink()
            lots = self.env['stock.lot']
            for index in range(2):
                lot = self.env['stock.lot'].create({'name':'SSC-'+tracking+str(index),'product_id':product.id,'company_id':self.company.id})
                lots |= lot
                self.env['stock.move.line'].create({'move_id':move.id,'picking_id':picking.id,'product_id':product.id,
                    'uom_id':move.uom_id.id,'quantity':1,'lot_id':lot.id,'location_id':move.location_id.id,'location_dest_id':move.location_dest_id.id})
            move.picked = True
            picking.with_context(skip_backorder=True).button_validate()
            self.assertEqual(picking.state,'done')
            claim = self._claim(order)
            self.assert_qty(claim.claim_line_ids.receipt_gross_qty,2)
            self.assertEqual(set(move.move_line_ids.lot_id.ids),set(lots.ids))
            self.assertEqual(claim.receipt_move_count,1)

    def test_untracked_goods_use_validated_movements_but_manual_methods_do_not(self):
        order = self._order(10,products=[self.untracked])
        self._validate(self._incoming(order))
        line = self._claim(order).claim_line_ids
        self.assert_qty(line.receipt_gross_qty,10)
        order.order_line.qty_received_method = 'manual'
        self._refresh(line.claim_id)
        self.assertFalse(line.receiving_complete)
        self.assertEqual(line.receiving_status,'requires_review')

    def test_services_are_not_physical_receiving_evidence(self):
        service = self.env['product.product'].create({'name':'SSC Receipt Service','type':'service'})
        order = self._order(10,products=[service])
        result = order.order_line.with_user(self.user)._ssc_receiving_analysis()[order.order_line.id]
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'],'requires_review')

    def test_navigation_only_related_documents_and_evidence_view_is_readonly(self):
        order = self._order(10)
        self._validate(self._incoming(order),5)
        other = self._order(10)
        claim = self._claim(order)
        action = claim.action_view_related_receipts()
        records = self.env['stock.picking'].with_user(self.user).search(action['domain'])
        self.assertEqual(set(records.ids),set(order.picking_ids.ids))
        self.assertFalse(records & other.picking_ids)
        moves = claim.action_view_receiving_movements()
        self.assertEqual(moves['res_model'],'stock.move')
        with self.assertRaises(AccessError):
            claim.claim_line_ids.write({'receipt_gross_qty':999})
        with self.assertRaises(AccessError):
            claim.write({'review_receipt_signature':'forged'})
        with self.assertRaises(AccessError):
            order.order_line.move_ids[:1].with_user(self.user).write({'ssc_completed_purchase_qty':999})
        self.assertIn('receiving_summary',claim.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_claim_form').id,'form')['arch'])
        with Form(claim) as form:
            self.assertEqual(form.purchase_id,order)

    def test_no_stock_permission_is_unavailable_not_false_zero(self):
        order = self._order(10)
        self._validate(self._incoming(order),5)
        self.env['ir.access'].create({'name':'SSC test no stock visibility','model_id':self.env['ir.model']._get_id('stock.picking'),
            'operation':'r','domain':"[] if user.id != %s else [('id', '=', 0)]" % self.no_stock.id})
        claim = self._claim(order,user=self.no_stock)
        self.assertEqual(claim.claim_line_ids.receiving_status,'unavailable')
        self.assertFalse(claim.receiving_accessible)
        with self.assertRaises(AccessError):
            claim.action_view_related_receipts()

    def test_warehouse_record_restriction_detects_incomplete_coverage(self):
        order = self._order(100)
        self._validate(self._incoming(order),50)
        second_wh = self.env['stock.warehouse'].create({'name':'SSC Restricted Warehouse','code':'SSCRW','company_id':self.company.id})
        picking = self.env['stock.picking'].create({'picking_type_id':second_wh.in_type_id.id,'partner_id':self.supplier.id,
            'location_id':self.vendor_location.id,'location_dest_id':second_wh.lot_stock_id.id})
        self.env['stock.move'].create({'product_id':self.product.id,'product_uom_qty':10,'uom_id':self.product.uom_id.id,
            'purchase_line_id':order.order_line.id,'picking_id':picking.id,'location_id':self.vendor_location.id,'location_dest_id':second_wh.lot_stock_id.id})
        picking.action_confirm()
        self._validate(picking,10)
        self.env['ir.access'].create({'name':'SSC test warehouse limitation','model_id':self.env['ir.model']._get_id('stock.move'),
            'operation':'r','domain':"[] if user.id != %s else [('picking_type_id.warehouse_id', '!=', %s)]" % (self.user.id,second_wh.id)})
        claim = self._claim(order)
        self.assertFalse(claim.claim_line_ids.receiving_complete)
        self.assertEqual(claim.claim_line_ids.receiving_status,'unavailable')
        self.assertFalse(claim.receipt_move_preview_ids)

    def test_cross_company_claim_and_receipt_navigation_are_protected(self):
        other_wh = self.env['stock.warehouse'].search([('company_id','=',self.other.id)],limit=1)
        order = self._order(10,warehouse=other_wh)
        self._validate(self._incoming(order))
        claim = self._claim(order,user=self.multi)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,10)
        action = claim.with_context(allowed_company_ids=[self.other.id]).action_view_related_receipts()
        self.assertEqual(set(self.env['stock.picking'].with_user(self.multi).search(action['domain']).ids),set(order.picking_ids.ids))
        with self.assertRaises(AccessError):
            claim.with_user(self.user).action_view_related_receipts()
        with self.assertRaises(AccessError):
            claim.with_user(self.multi).with_context(allowed_company_ids=[self.company.id]).read(['receipt_count'])

    def test_receipt_source_changes_warn_without_rewriting_review_or_financial_history(self):
        order = self._order(100)
        self._validate(self._incoming(order),70)
        claim = self._claim(order)
        submit_prepared(claim)
        self.assertTrue(claim.review_receipt_signature)
        snapshot = deepcopy(claim.claim_line_ids.valuation_snapshot)
        events = claim.audit_event_ids.ids
        signature = claim.review_receipt_signature
        additional_receipt(order)
        self._refresh(claim)
        self.assertTrue(claim.receiving_review_warning)
        self.assertEqual(claim.review_receipt_signature,signature)
        self.assertEqual(claim.claim_line_ids.valuation_snapshot,snapshot)
        self.assertTrue(set(events)<=set(claim.audit_event_ids.ids))
        added=claim.audit_event_ids.filtered(lambda event:event.id not in events)
        self.assertEqual(added.mapped('event_type'),['readiness_invalidated'])
        self.assertEqual(claim.state,'under_review')

    def test_reads_do_not_change_stock_accounting_or_claim_authorization(self):
        order = self._order(10)
        self._validate(self._incoming(order),5)
        claim = self._claim(order)
        self.env.flush_all()
        moves = order.order_line.move_ids
        before = moves.read(['quantity','product_uom_qty','state','location_id','location_dest_id','write_date'])
        quants = self.env['stock.quant'].search([('product_id','=',self.product.id)])
        quantities = quants.read(['quantity','reserved_quantity','location_id','write_date'])
        accounting = self.env['account.move'].search_count([])
        event_ids = claim.audit_event_ids.ids
        for _ in range(2):
            self._refresh(claim)
            self.assertTrue(claim.receiving_summary)
            claim.action_view_related_receipts()
            claim.action_view_receiving_movements()
        self.assertEqual(moves.read(['quantity','product_uom_qty','state','location_id','location_dest_id','write_date']),before)
        self.assertEqual(quants.read(['quantity','reserved_quantity','location_id','write_date']),quantities)
        self.assertEqual(self.env['account.move'].search_count([]),accounting)
        self.assertEqual(set(claim.audit_event_ids.ids),set(event_ids))
        self.assertEqual(claim.state,'draft')

    def test_many_lines_aggregate_once_per_source_and_keep_preview_bounded(self):
        order = self._order(3,products=[self.product]*10)
        self._validate(self._incoming(order))
        claim = self._claim(order)
        self.assertEqual(claim.receipt_count,1)
        self.assertEqual(claim.receipt_move_count,10)
        self.assertLessEqual(len(claim.receipt_move_preview_ids),40)
        for line in claim.claim_line_ids:
            self.assert_qty(line.receipt_gross_qty,3)


    def test_internal_receipt_leg_return_keeps_supplier_provenance(self):
        order,claim = self._multi_step('two_steps',1)
        internals = order.order_line.move_ids.move_dest_ids.filtered(lambda move: move.state == 'done' and move.location_id.usage == 'internal')
        self.assertTrue(internals)
        returned = internals[:1].picking_id._create_return()
        returned.write({'location_dest_id':self.vendor_location.id})
        returned.move_ids.write({'location_dest_id':self.vendor_location.id})
        self._validate(returned,5)
        line = self._refresh(claim)
        self.assert_qty(line.receipt_gross_qty,80)
        self.assert_qty(line.receipt_return_qty,5)
        self.assert_qty(line.receipt_net_qty,75)
        self.assertEqual(returned.move_ids.ssc_receipt_kind,'return')

    def test_incompatible_and_nonfinite_quantity_is_never_receiving_evidence(self):
        order = self._order(10)
        move = self._incoming(order).move_ids
        incompatible = self.env['uom.uom'].create({'name':'SSC unrelated receipt unit'})
        move.uom_id = incompatible
        claim = self._claim(order)
        self.assertEqual(claim.claim_line_ids.receiving_status,'requires_review')
        self.assertFalse(claim.claim_line_ids.receiving_complete)
        for quantity in (float('nan'),float('inf'),-1):
            with self.assertRaises(ValidationError):
                move._ssc_normalized_quantity(order.order_line,quantity)

    def test_hidden_internal_provenance_is_incomplete_not_partial_success(self):
        order,claim = self._multi_step('two_steps',1)
        self.env['ir.access'].create({'name':'SSC test hidden internal provenance',
            'model_id':self.env['ir.model']._get_id('stock.move'), 'operation':'r',
            'domain':"[] if user.id != %s else [('location_id.usage', '!=', 'internal')]" % self.user.id})
        self._refresh(claim)
        self.assertEqual(claim.claim_line_ids.receiving_status,'unavailable')
        self.assertFalse(claim.receipt_move_preview_ids)
        with self.assertRaises(AccessError):
            claim.action_view_related_receipts()


    def test_source_line_reparenting_requires_review(self):
        order = self._order(10)
        self._validate(self._incoming(order))
        claim = self._claim(order)
        line = order.order_line
        other = self._order(10)
        line.order_id = other
        self._refresh(claim)
        self.assertEqual(claim.claim_line_ids.receiving_status,'requires_review')
        self.assertFalse(claim.claim_line_ids.receiving_complete)
        self.assertFalse(claim.claim_line_ids.receiving_basis['move_ids'])
