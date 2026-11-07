from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from copy import deepcopy

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import new_test_user

from .test_receipt_integration import ReceiptIntegrationCase


class TestReceivingExceptions(ReceiptIntegrationCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ordinary = new_test_user(cls.env, login='ssc_exception_preparer',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,stock.group_stock_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.warehouse_user = new_test_user(cls.env, login='ssc_exception_warehouse',
            groups='supplier_shortage_claim_reconciliation.group_warehouse_user,purchase.group_purchase_user,stock.group_stock_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.combined = new_test_user(cls.env, login='ssc_exception_combined_roles',
            groups='supplier_shortage_claim_reconciliation.group_warehouse_user,supplier_shortage_claim_reconciliation.group_accounts_reviewer,supplier_shortage_claim_reconciliation.group_module_admin,purchase.group_purchase_user,stock.group_stock_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.admin_viewer = new_test_user(cls.env, login='ssc_exception_config_admin',
            groups='supplier_shortage_claim_reconciliation.group_module_admin,purchase.group_purchase_user,stock.group_stock_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])

    def _scan(self, claim):
        self._refresh(claim)
        claim.action_reconcile_receiving()
        claim.invalidate_recordset(['receiving_exception_ids', 'receiving_exception_count'])
        return claim.receiving_exception_ids

    def _exception(self, claim, category):
        return claim.receiving_exception_ids.filtered(lambda rec: rec.exception_type == category)

    def test_successive_backorders_count_only_active_remaining_demand(self):
        order = self._order(100)
        first = self._validate(self._incoming(order),40)
        second = self._validate(self._incoming(order),30)
        pending = self._incoming(order)
        claim = self._claim(order)
        self._scan(claim)
        self.assertEqual(second.backorder_id,first)
        self.assertEqual(pending.backorder_id,second)
        self.assert_qty(claim.claim_line_ids.receipt_gross_qty,70)
        self.assert_qty(claim.claim_line_ids.receipt_pending_qty,30)
        self.assert_qty(self._exception(claim,'pending_delivery').affected_quantity,30)
        self.assertEqual(claim.state,'draft')
        self._validate(pending)
        self._scan(claim)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,100)
        self.assertFalse(self._exception(claim,'pending_delivery').condition_present)
        self.assertTrue(self._exception(claim,'receiving_source_changed'))

    def test_partial_then_canceled_backorder_keeps_received_and_historical_demand(self):
        order = self._order(100)
        self._validate(self._incoming(order),40)
        self._validate(self._incoming(order),30)
        backorder = self._incoming(order)
        backorder.action_cancel()
        claim = self._claim(order)
        self._scan(claim)
        canceled = self._exception(claim,'canceled_backorder_review')
        self.assertEqual(len(canceled),1)
        self.assert_qty(canceled.affected_quantity,30)
        self.assertEqual(canceled.source_move_ids,backorder.move_ids)
        self.assert_qty(claim.claim_line_ids.receipt_delivery_difference_qty,30)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,70)
        self.assertFalse(self._exception(claim,'pending_delivery'))
        self.assertEqual(claim.state,'draft')

    def test_canceled_operation_replaced_by_new_receipt_remains_historical_review(self):
        order = self._order(100)
        receipt = self._validate(self._incoming(order),70)
        self._incoming(order).action_cancel()
        extra = receipt.copy({'backorder_id':False})
        extra.action_confirm()
        self._validate(extra,30)
        claim = self._claim(order)
        self._scan(claim)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,100)
        self.assert_qty(claim.claim_line_ids.receipt_delivery_difference_qty,0)
        self.assertTrue(self._exception(claim,'canceled_backorder_review'))

    def test_overreceipt_and_unchanged_refresh_are_idempotent(self):
        order = self._order(100)
        self._validate(self._incoming(order),105)
        claim = self._claim(order)
        exceptions = self._scan(claim)
        over = self._exception(claim,'over_receipt')
        self.assert_qty(over.affected_quantity,5)
        self.assert_qty(claim.claim_line_ids.receipt_delivery_difference_qty,0)
        self.assert_qty(claim.claim_line_ids.receipt_over_received_qty,5)
        before = exceptions.read(['source_key','source_signature','last_detected_at','review_state'])
        events = exceptions.audit_event_ids.ids
        for unused in range(3):
            self._scan(claim)
        self.assertEqual(exceptions.read(['source_key','source_signature','last_detected_at','review_state']),before)
        self.assertEqual(set(exceptions.audit_event_ids.ids),set(events))
        self.assertEqual(claim.receiving_exception_ids,exceptions)

    def test_return_policy_difference_is_explained_without_false_conflict(self):
        for refundable in (False,True):
            order = self._order(100)
            receipt = self._validate(self._incoming(order))
            returned = receipt._create_return()
            returned.move_ids.to_refund = refundable
            self._validate(returned,10)
            claim = self._claim(order)
            self._scan(claim)
            self.assert_qty(claim.claim_line_ids.receipt_net_qty,90)
            self.assert_qty(claim.claim_line_ids.receipt_delivery_difference_qty,10)
            self.assertFalse(self._exception(claim,'source_quantity_mismatch'))
            self.assertFalse(self._exception(claim,'over_receipt'))
            self.assertTrue(claim.claim_line_ids.receiving_integrity_note)

    def test_corrupted_native_received_value_is_reported_without_overwrite(self):
        order = self._order(10)
        self._validate(self._incoming(order))
        claim = self._claim(order)
        order.order_line.qty_received = 123  # Simulated external integration defect, not a stock completion fixture.
        self._scan(claim)
        exception = self._exception(claim,'source_quantity_mismatch')
        self.assertTrue(exception)
        self.assertFalse(exception.quantity_known)
        self.assert_qty(order.order_line.qty_received,123)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,10)
        order.order_line.qty_received = 10
        self._scan(claim)
        self.assertFalse(exception.condition_present)

    def test_unmatched_move_in_verified_native_picking_is_not_guessed(self):
        order = self._order(10)
        receipt = self._incoming(order)
        orphan = self.env['stock.move'].create({'product_id':self.product.id,'uom_id':self.product.uom_id.id,
            'product_uom_qty':3,'picking_id':receipt.id,'location_id':receipt.location_id.id,'location_dest_id':receipt.location_dest_id.id})
        receipt.action_confirm()
        self._validate(receipt)
        # Native purchase_stock repairs a same-product orphan during validation;
        # simulate a later lost linkage, without fabricating movement completion.
        orphan.purchase_line_id = False
        claim = self._claim(order)
        self._scan(claim)
        exception = self._exception(claim,'missing_purchase_line')
        self.assertEqual(exception.source_move_ids,orphan)
        self.assertFalse(exception.purchase_line_id)
        self.assertFalse(exception.quantity_known)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,10)

    def test_incompatible_uom_creates_unknown_quantity_review(self):
        order = self._order(10)
        self._incoming(order).move_ids.uom_id = self.env['uom.uom'].create({'name':'SSC Unsupported Receipt Dimension'})
        claim = self._claim(order)
        self._scan(claim)
        exception = self._exception(claim,'incompatible_uom')
        self.assertTrue(exception)
        self.assertFalse(exception.quantity_known)
        self.assertFalse(claim.claim_line_ids.receiving_complete)

    def test_authorized_review_assignment_decision_and_immutable_history(self):
        order = self._order(10)
        claim = self._claim(order)
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        exception.write({'assigned_user_id':self.warehouse_user.id})
        warehouse = exception.with_user(self.warehouse_user)
        warehouse.action_start_review()
        warehouse.write({'review_reason':'Warehouse checked: supplier delivery remains scheduled.'})
        with self.assertRaises(AccessError):
            warehouse.action_resolve()
        exception.action_resolve()
        self.assertEqual(exception.review_state,'resolved')
        self.assertEqual(exception.resolved_by,self.user)
        self.assertTrue(exception.resolved_at)
        event = exception.audit_event_ids.filtered(lambda event: event.event_type == 'exception_resolved')
        self.assertEqual(event.actor_id,self.user)
        self.assertIn('Warehouse checked',event.reason)
        self.assertTrue(event.valuation_basis['source_move_ids'])
        with self.assertRaises(AccessError):
            event.with_user(self.ordinary).write({'actor_id':self.ordinary.id})
        with self.assertRaises(AccessError):
            event.with_user(self.ordinary).unlink()

    def test_decision_requires_reason_and_rejects_config_role_escalation(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        count = len(exception.audit_event_ids)
        with self.assertRaises(ValidationError):
            exception.action_dismiss()
        for user in (self.ordinary,self.admin_viewer,self.combined):
            with self.assertRaises(AccessError):
                exception.with_user(user).action_dismiss()
        with self.assertRaises(AccessError):
            exception.with_user(self.ordinary).write({'review_state':'resolved'})
        with self.assertRaises(AccessError):
            exception.copy()
        with self.assertRaises(AccessError):
            exception.unlink()
        with self.assertRaises(AccessError):
            self.env['supplier.shortage.receipt.exception'].with_user(self.user).create({'name':'Forged'})
        with self.assertRaises(AccessError):
            self.env['supplier.shortage.claim.audit'].with_user(self.user).create({'receipt_exception_id':exception.id,'actor_id':self.user.id})
        self.assertEqual(len(exception.audit_event_ids),count)
        self.assertEqual(exception.review_state,'new')

    def test_transaction_rollback_removes_success_decision_event(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        before = set(exception.audit_event_ids.ids)
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.env.cr.savepoint():
                exception.write({'review_reason':'Reviewed delivery plan and supplier schedule.'})
                exception.action_dismiss()
                raise RuntimeError('rollback')
        exception.invalidate_recordset()
        self.assertEqual(exception.review_state,'new')
        self.assertEqual(set(exception.audit_event_ids.ids),before)

    def test_resolved_anomaly_reopens_only_on_material_source_change(self):
        order = self._order(100)
        receipt = self._validate(self._incoming(order),105)
        claim = self._claim(order)
        self._scan(claim)
        exception = self._exception(claim,'over_receipt')
        exception.write({'review_reason':'Authorized team reviewed the extra delivered goods.'})
        exception.action_resolve()
        actor,when = exception.resolved_by,exception.resolved_at
        self._scan(claim)
        self.assertEqual(exception.review_state,'resolved')
        returned = receipt._create_return()
        self._validate(returned,10)
        self._scan(claim)
        self.assertFalse(exception.condition_present)
        replacement = returned._create_return()
        self._validate(replacement,15)
        self._scan(claim)
        self.assertEqual(exception.review_state,'new')
        self.assertTrue(exception.condition_present)
        self.assertEqual(exception.resolved_by,actor)
        self.assertEqual(exception.resolved_at,when)
        self.assert_qty(exception.affected_quantity,10)
        self.assertTrue(exception.audit_event_ids.filtered(lambda event: event.event_type == 'exception_reopened'))

    def test_review_claim_source_change_preserves_proposal_and_financial_history(self):
        order = self._order(100)
        self._validate(self._incoming(order),70)
        claim = self._claim(order)
        submit_prepared(claim)
        self._scan(claim)
        snapshot = deepcopy(claim.claim_line_ids.valuation_snapshot)
        proposal = claim.claim_line_ids.proposed_qty
        history = set(claim.audit_event_ids.ids)
        additional_receipt(order)
        self._scan(claim)
        self.assertTrue(self._exception(claim,'receiving_source_changed'))
        self.assertTrue(claim.receiving_review_warning)
        self.assertEqual(claim.state,'under_review')
        self.assertEqual(claim.claim_line_ids.valuation_snapshot,snapshot)
        self.assertEqual(claim.claim_line_ids.proposed_qty,proposal)
        self.assertTrue(history<=set(claim.audit_event_ids.ids))
        added=claim.audit_event_ids.filtered(lambda event:event.id not in history)
        self.assertEqual(added.mapped('event_type'),['readiness_invalidated'])

    def test_mixed_batch_preserves_duplicate_product_line_identity(self):
        first = self._order(10,products=[self.product,self.product,self.second])
        second = self._order(20)
        receipt = self._incoming(first)
        for index,move in enumerate(receipt.move_ids.sorted('purchase_line_id'),start=1):
            move.quantity = index
            move.picked = True
        receipt.with_context(skip_backorder=True).button_validate()
        self.assertEqual(receipt.state,'done')
        claim_a,claim_b = self._claim(first),self._claim(second)
        claims = claim_a | claim_b
        claims.action_reconcile_receiving()
        self.assertEqual(len(claim_a.receiving_exception_ids.filtered(lambda rec: rec.exception_type == 'pending_delivery')),3)
        self.assertEqual(len(claim_b.receiving_exception_ids.filtered(lambda rec: rec.exception_type == 'pending_delivery')),1)
        self.assertEqual(len(set(claim_a.receiving_exception_ids.purchase_line_id.ids)),3)
        for line in claim_a.claim_line_ids:
            self.assertEqual(self._exception(claim_a,'pending_delivery').filtered(lambda rec: rec.purchase_line_id == line.purchase_line_id).company_id,self.company)

    def test_company_isolation_applies_to_search_export_history_and_actions(self):
        other_wh = self.env['stock.warehouse'].search([('company_id','=',self.other.id)],limit=1)
        order = self._order(10,warehouse=other_wh)
        claim = self._claim(order,user=self.multi)
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        restricted = exception.with_user(self.user)
        self.assertFalse(self.env['supplier.shortage.receipt.exception'].with_user(self.user).search([('id','=',exception.id)]))
        with self.assertRaises(AccessError):
            restricted.read(['affected_quantity'])
        with self.assertRaises(AccessError):
            restricted.action_open_sources()
        with self.assertRaises(AccessError):
            restricted.write({'review_reason':'Unauthorized cross-company correction.'})
        with self.assertRaises(AccessError):
            exception.audit_event_ids.with_user(self.user).read(['change_summary'])
        self.assertEqual(exception.with_user(self.multi).company_id,self.other)

    def test_stock_record_restriction_hides_persisted_exception_quantities(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        self.env['ir.access'].create({'name':'SSC exception evidence restriction','model_id':self.env['ir.model']._get_id('stock.move'),
            'operation':'r','domain':"[] if user.id != %s else [('id','=',0)]" % self.ordinary.id})
        ordinary = exception.with_user(self.ordinary)
        self.assertFalse(self.env['supplier.shortage.receipt.exception'].with_user(self.ordinary).search([('id','=',exception.id)]))
        with self.assertRaises(AccessError):
            ordinary.read(['affected_quantity'])
        with self.assertRaises(AccessError):
            exception.audit_event_ids.with_user(self.ordinary).read(['valuation_basis'])
        before = exception.source_signature
        with self.assertRaises(AccessError):
            claim.with_user(self.ordinary).action_reconcile_receiving()
        self.assertEqual(exception.source_signature,before)

    def test_refresh_does_not_write_purchase_stock_accounting_or_send_messages(self):
        order = self._order(100)
        self._validate(self._incoming(order),70)
        claim = self._claim(order)
        moves = order.order_line.move_ids
        quants = self.env['stock.quant'].search([('product_id','=',self.product.id)])
        source = (order.read(['state','amount_total','write_date']),order.order_line.read(['product_qty','price_unit','write_date']),
                  moves.read(['state','quantity','product_uom_qty','write_date']),quants.read(['quantity','reserved_quantity','write_date']))
        accounting = self.env['account.move'].search_count([])
        messages = self.env['mail.message'].search_count([])
        self._scan(claim)
        self._scan(claim)
        self.assertEqual((order.read(['state','amount_total','write_date']),order.order_line.read(['product_qty','price_unit','write_date']),
                  moves.read(['state','quantity','product_uom_qty','write_date']),quants.read(['quantity','reserved_quantity','write_date'])),source)
        self.assertEqual(self.env['account.move'].search_count([]),accounting)
        self.assertEqual(self.env['mail.message'].search_count([]),messages)
        self.assertEqual(claim.state,'draft')

    def test_workbench_views_and_source_navigation_load(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        for kind in ('list','search','form'):
            view = self.env.ref('supplier_shortage_claim_reconciliation.view_receiving_exception_' + kind)
            self.assertIn('exception',exception.get_view(view.id,kind)['arch'])
        with Form(exception) as form:
            self.assertEqual(form.purchase_id,claim.purchase_id)
        action = exception.action_open_sources()
        self.assertEqual(set(self.env['stock.move'].with_user(self.user).search(action['domain']).ids),set(exception.source_move_ids.ids))
        self.assertIn('Receiving Integrity Summary',claim.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_claim_form').id,'form')['arch'])
        self.assertEqual(claim.action_view_receiving_exceptions()['res_model'],exception._name)


    def test_inconsistent_backorder_lineage_requires_review_not_overlapping_demand(self):
        order = self._order(100)
        self._validate(self._incoming(order),70)
        other = self._order(10)
        pending = self._incoming(order)
        pending.backorder_id = self._incoming(other)
        claim = self._claim(order)
        self._scan(claim)
        self.assertTrue(self._exception(claim,'inconsistent_backorder'))
        self.assertFalse(self._exception(claim,'pending_delivery'))
        self.assertFalse(claim.claim_line_ids.receiving_complete)

    def test_assignment_rejects_other_company_and_configuration_only_employee(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        with self.assertRaises(ValidationError):
            exception.write({'assigned_user_id':self.admin_viewer.id})
        self.assertEqual(exception.assigned_user_id,self.user)
        with self.assertRaises(AccessError):
            exception.with_user(self.ordinary).write({'assigned_user_id':self.ordinary.id})

    def test_unchanged_decision_action_does_not_append_duplicate_success_event(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        exception.write({'review_reason':'Buyer checked and accepted the scheduled delivery.'})
        exception.action_dismiss()
        before = set(exception.audit_event_ids.ids)
        with self.assertRaises(ValidationError):
            exception.action_dismiss()
        self.assertEqual(set(exception.audit_event_ids.ids),before)
        self._scan(claim)
        self.assertEqual(exception.review_state,'dismissed')


    def test_warehouse_native_done_quantity_correction_is_detected_on_refresh(self):
        order = self._order(100)
        receipt = self._validate(self._incoming(order))
        claim = self._claim(order)
        self._scan(claim)
        # Supported Inventory move-line correction; never performed by APP 05.
        receipt.move_ids.move_line_ids.with_user(self.user).write({'quantity':95})
        self._scan(claim)
        self.assert_qty(claim.claim_line_ids.receipt_net_qty,95)
        self.assert_qty(claim.claim_line_ids.receipt_delivery_difference_qty,5)
        self.assertTrue(self._exception(claim,'receiving_source_changed'))
        self.assertEqual(claim.state,'draft')
        self.assert_qty(claim.claim_line_ids.proposed_qty,1)

    def test_manual_reopen_requires_new_reason_and_preserves_original_decision(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        exception.write({'review_reason':'Supplier delivery plan checked by purchase team.'})
        exception.action_resolve()
        original = exception.last_decision_reason
        with self.assertRaises(ValidationError):
            exception.action_reopen()
        with self.assertRaises(ValidationError):
            exception.with_user(self.ordinary).write({'review_reason':'Unapproved reopening notes.'})
        exception.write({'review_reason':'Warehouse reported a later source correction; revalidate delivery.'})
        exception.action_reopen()
        self.assertEqual(exception.review_state,'new')
        self.assertEqual(exception.last_decision_reason,original)
        self.assertTrue(exception.audit_event_ids.filtered(lambda event: event.event_type == 'exception_reopened'))


    def test_warehouse_permission_change_hides_persisted_quantities_and_history(self):
        claim = self._claim(self._order(10))
        self._scan(claim)
        exception = self._exception(claim,'pending_delivery')
        self.env['ir.access'].create({'name':'SSC warehouse visibility changed','model_id':self.env['ir.model']._get_id('stock.warehouse'),
            'operation':'r','domain':"[] if user.id != %s else [('id','!=',%s)]" % (self.ordinary.id,self.wh.id)})
        with self.assertRaises(AccessError):
            exception.with_user(self.ordinary).read(['affected_quantity'])
        with self.assertRaises(AccessError):
            exception.audit_event_ids.with_user(self.ordinary).read(['valuation_basis'])
        with self.assertRaises(AccessError):
            claim.with_user(self.ordinary).action_reconcile_receiving()
