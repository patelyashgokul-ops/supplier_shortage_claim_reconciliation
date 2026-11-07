from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from copy import deepcopy
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger


class TestPurchaseIntegration(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other = cls.env['res.company'].create({'name': 'SSC Purchase Company B'})
        cls.supplier = cls.env['res.partner'].create({'name': 'SSC Purchase Shared Supplier'})
        cls.other_supplier = cls.env['res.partner'].create({'name': 'SSC Purchase Other Supplier'})
        cls.speaker = cls.env['product.product'].create({'name': 'SSC Speaker', 'type': 'consu'})
        cls.charger = cls.env['product.product'].create({'name': 'SSC Charger', 'type': 'consu'})
        cls.cable = cls.env['product.product'].create({'name': 'SSC Cable', 'type': 'consu'})
        cls.service = cls.env['product.product'].create({'name': 'SSC Service', 'type': 'service'})
        cls.user = new_test_user(cls.env, login='ssc_purchase_integration_user',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,base.group_allow_export',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.manager = new_test_user(cls.env, login='ssc_purchase_integration_manager',
            groups='supplier_shortage_claim_reconciliation.group_claim_manager,purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.buyer = new_test_user(cls.env, login='ssc_purchase_integration_buyer', groups='purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.viewer = new_test_user(cls.env, login='ssc_purchase_integration_no_po',
            groups='supplier_shortage_claim_reconciliation.group_warehouse_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.multi = new_test_user(cls.env, login='ssc_purchase_integration_multi',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user,base.group_multi_company',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id, cls.other.id])])
        cls.po = cls._order()
        # Real native confirmation is fixture setup, never a claim integration side effect.
        cls.po.button_confirm()
        cls.second = cls._order()
        cls.second.button_confirm()
        cls.foreign = cls._order(company=cls.other)
        cls.foreign.button_confirm()

    @classmethod
    def _order(cls, company=None, supplier=None):
        return cls.env['purchase.order'].with_company(company or cls.company).create({
            'partner_id': (supplier or cls.supplier).id, 'user_id': cls.buyer.id if not company else False,
            'order_line': [Command.create({'product_id': product.id, 'product_qty': quantity, 'price_unit': price,
                'discount': 0, 'tax_ids': [Command.clear()]}) for product,quantity,price in
                [(cls.speaker,100,500),(cls.charger,200,200),(cls.cable,300,10)]]})

    def _claim(self, order=None, lines=None, user=None, **values):
        order = order or self.po
        vals = {'purchase_id': order.id}
        if lines is None:
            lines = order.order_line[:1]
        vals['claim_line_ids'] = [Command.create({'purchase_line_id': line.id, 'proposed_qty': 15}) for line in lines]
        vals.update(values)
        return self.env['supplier.shortage.claim'].with_user(user or self.user).create(vals)

    def test_confirmed_po_defaults_and_source_context(self):
        claim = self._claim()
        self.assertEqual(claim.partner_id, self.supplier)
        self.assertFalse(claim.partner_id.company_id)
        self.assertEqual(claim.company_id, self.company)
        self.assertEqual(claim.currency_id, self.po.currency_id)
        self.assertEqual(claim.purchase_buyer_id, self.buyer)
        self.assertEqual(claim.user_id, self.user)
        self.assertEqual(claim.purchase_state, 'purchase')
        self.assertTrue(claim.purchase_date)
        submit_prepared(claim)
        self.assertEqual(claim.state, 'under_review')
        self.assertEqual(claim.purchase_source_status, 'current')

    def test_supplier_policy_is_exact_partner_not_similar_name_or_contact(self):
        similar = self.env['res.partner'].create({'name': self.supplier.name})
        contact = self.env['res.partner'].create({'name': 'Supplier contact', 'parent_id': self.supplier.id})
        for partner in (similar, contact, self.other_supplier):
            with self.assertRaises(ValidationError):
                self._claim(partner_id=partner.id)

    def test_multiline_selection_and_distinct_source_quantities(self):
        claim = self._claim(lines=self.po.order_line[0] | self.po.order_line[2])
        self.assertEqual(len(claim.claim_line_ids), 2)
        self.assertEqual(claim.claim_line_ids.purchase_line_id, self.po.order_line[0] | self.po.order_line[2])
        for line in claim.claim_line_ids:
            self.assertEqual(line.product_id, line.purchase_line_id.product_id)
            self.assertEqual(line.product_uom_id, line.purchase_line_id.uom_id)
            self.assertEqual(line.product_uom_id.compare(line.purchase_qty, line.purchase_line_id.product_qty), 0)
            self.assertEqual(line.currency_id.compare_amounts(line.source_unit_price, line.purchase_line_id.price_unit), 0)
            self.assertEqual(line.currency_id.compare_amounts(line.provisional_untaxed_amount, 15 * line.purchase_line_id.price_unit), 0)
        submit_prepared(claim)
        self.assertEqual(claim.state, 'under_review')

    def test_same_product_on_two_lines_remains_distinct(self):
        extra = self.env['purchase.order.line'].create({'order_id': self.po.id, 'product_id': self.speaker.id,
            'product_qty': 25, 'price_unit': 450, 'tax_ids': [Command.clear()]})
        claim = self._claim(lines=self.po.order_line[:1] | extra)
        self.assertEqual(len(claim.claim_line_ids.purchase_line_id), 2)
        self.assertEqual(len(claim.claim_line_ids.product_id), 1)
        self.assertEqual(len(self._claim(lines=extra).claim_line_ids), 1)

    def test_other_po_same_supplier_and_different_supplier_cannot_be_mixed(self):
        with self.assertRaises(ValidationError):
            self._claim(lines=self.second.order_line[:1])
        different = self._order(supplier=self.other_supplier)
        with self.assertRaises(ValidationError):
            self._claim(order=different, partner_id=self.supplier.id)
        self.assertEqual(self._claim(order=self.second).purchase_id, self.second)

    def test_cross_company_relationships_and_activated_company_scopes(self):
        with self.assertRaises(AccessError):
            self._claim(order=self.foreign)
        with self.assertRaises(UserError):
            self._claim(order=self.foreign, user=self.multi, company_id=self.company.id)
        with self.assertRaises(UserError):
            self._claim(lines=self.foreign.order_line[:1], user=self.multi)
        foreign = self._claim(order=self.foreign, user=self.multi, user_id=False)
        self.assertEqual(foreign.company_id, self.other)
        with self.assertRaises(AccessError):
            foreign.with_user(self.user).action_open_purchase_order()
        with self.assertRaises(AccessError):
            self.foreign.with_user(self.multi).with_context(allowed_company_ids=[self.company.id]).action_view_shortage_claims()

    def test_nonphysical_sections_subsections_notes_services_and_downpayments(self):
        draft = self._order()
        unsupported = self.env['purchase.order.line']
        for kind in ('line_section','line_subsection','line_note'):
            unsupported |= self.env['purchase.order.line'].create({'order_id': draft.id, 'display_type': kind, 'name': kind, 'product_qty': 0})
        unsupported |= self.env['purchase.order.line'].create({'order_id': draft.id, 'product_id': self.service.id, 'product_qty': 1, 'price_unit': 100})
        unsupported |= self.env['purchase.order.line'].create({'order_id': draft.id, 'is_downpayment': True,
            'product_id': self.speaker.id, 'product_qty': 1, 'price_unit': 100})
        for line in unsupported:
            with self.subTest(line=line.id), self.assertRaises(ValidationError):
                self._claim(order=draft, lines=line)
        self.assertTrue(draft.order_line)

    def test_rfq_sent_approval_pending_and_cancelled_are_not_reviewable(self):
        for state in ('draft','sent','to approve','cancel'):
            draft = self._order()
            draft.state = state
            claim = self._claim(order=draft)
            with self.subTest(state=state), self.assertRaisesRegex(ValidationError, 'Confirm the Purchase Order'):
                claim.action_submit_for_review()
            self.assertEqual(claim.state, 'draft')
            self.assertFalse(claim.audit_event_ids.filtered(lambda event: event.event_type == 'submitted'))

    def test_create_action_is_unsaved_and_defaults_revalidated(self):
        order = self.po.with_user(self.user)
        before = self.env['supplier.shortage.claim'].search_count([])
        action = order.with_context(active_id=self.second.id, default_purchase_id=self.second.id,
            default_partner_id=self.other_supplier.id).action_create_shortage_claim()
        self.assertNotIn('res_id', action)
        self.assertEqual(action['context']['default_purchase_id'], order.id)
        self.assertEqual(action['context']['default_user_id'], self.user.id)
        self.assertEqual(self.env['supplier.shortage.claim'].search_count([]), before)
        model = self.env['supplier.shortage.claim'].with_user(self.user).with_context(**action['context'])
        with Form(model) as form:
            self.assertEqual(form.purchase_id, order)
            self.assertEqual(form.partner_id, self.supplier)
            self.assertFalse(form.claim_line_ids)
        self.assertEqual(form.record.state, 'draft')
        self.assertFalse(form.record.claim_line_ids)
        with self.assertRaises(ValidationError):
            model.with_context(default_partner_id=self.other_supplier.id).create({})

    def test_unauthorized_create_action_and_forged_source_defaults(self):
        for user in (self.buyer, self.viewer, self.env.ref('base.public_user')):
            with self.assertRaises(AccessError):
                self.po.with_user(user).action_create_shortage_claim()
        with self.assertRaises(AccessError):
            self.env['supplier.shortage.claim'].with_user(self.user).with_context(default_purchase_id=self.foreign.id).default_get(['purchase_id','partner_id'])
        with self.assertRaises(AccessError):
            self.env['supplier.shortage.claim'].with_user(self.user).with_context(default_purchase_id=self.foreign.id).create({})

    def test_smart_counts_and_navigation_are_access_aware(self):
        first = self._claim()
        self._claim()
        self._claim(order=self.second)
        order = self.po.with_user(self.user)
        self.assertEqual(order.shortage_claim_count, 2)
        action = order.action_view_shortage_claims()
        self.assertEqual(self.env['supplier.shortage.claim'].with_user(self.user).search(action['domain']), first | self.env['supplier.shortage.claim'].with_user(self.user).search([('purchase_id','=',order.id),('id','!=',first.id)]))
        self.assertEqual(self.po.with_user(self.buyer).shortage_claim_count, 0)
        with self.assertRaises(AccessError):
            self.po.with_user(self.buyer).action_view_shortage_claims()
        with self.assertRaises(AccessError):
            self.foreign.with_user(self.user).read(['shortage_claim_count'])
        with self.assertRaises(AccessError):
            self.foreign.with_user(self.user).action_view_shortage_claims()

    def test_empty_smart_button_returns_exact_empty_domain_without_creating(self):
        action = self.second.with_user(self.user).action_view_shortage_claims()
        self.assertEqual(self.second.with_user(self.user).shortage_claim_count, 0)
        self.assertFalse(self.env['supplier.shortage.claim'].with_user(self.user).search(action['domain']))

    def test_claim_and_line_navigation_open_exact_sources(self):
        claim = self._claim(lines=self.po.order_line[0] | self.po.order_line[2])
        self.assertEqual(claim.action_open_purchase_order()['res_id'], self.po.id)
        for line in claim.claim_line_ids:
            self.assertEqual(line.action_open_purchase_line()['res_id'], line.purchase_line_id.id)
        with self.assertRaises(AccessError):
            claim.with_user(self.viewer).action_open_purchase_order()
        with self.assertRaises(AccessError):
            claim.claim_line_ids[:1].with_user(self.viewer).action_open_purchase_line()

    def test_reading_claim_without_native_po_rights_has_safe_source_status(self):
        claim = self._claim()
        source = claim.with_user(self.viewer)
        self.assertEqual(source.purchase_source_status, 'unavailable')
        self.assertFalse(source.purchase_buyer_id)
        self.assertFalse(source.purchase_currency_id)
        self.assertEqual(source.claim_line_ids.valuation_status, 'needs_review')
        self.assertFalse(source.claim_line_ids.currency_id)
        submit_prepared(claim)
        self.assertEqual(source.purchase_source_status, 'unavailable')
        self.assertFalse(source.claim_line_ids)
        with self.assertRaises(AccessError):
            claim.claim_line_ids.with_user(self.viewer).read(['preparation_snapshot'])
        with self.assertRaises(AccessError):
            claim.audit_event_ids.with_user(self.viewer).read(['valuation_basis'])

    def test_cancelled_source_keeps_history_and_submitted_value(self):
        claim = self._claim()
        submit_prepared(claim)
        snapshot = deepcopy(claim.claim_line_ids.valuation_snapshot)
        for bill in self.po.invoice_ids.filtered(lambda move:move.state!='cancel'):
            if bill.state=='posted':
                bill.button_draft()
            bill.button_cancel()
        self.po.button_cancel()
        self.assertEqual(claim.state, 'under_review')
        self.assertEqual(claim.purchase_source_status, 'changed')
        self.assertTrue(claim.purchase_source_warning)
        self.assertEqual(claim.claim_line_ids.valuation_snapshot, snapshot)
        self.assertTrue(claim.audit_event_ids)
        claim.with_user(self.manager).action_return_to_draft()
        with self.assertRaises(ValidationError):
            claim.action_submit_for_review()

    def test_price_quantity_supplier_and_currency_changes_are_visible_not_rewritten(self):
        claim = self._claim()
        submit_prepared(claim)
        snapshot = deepcopy(claim.claim_line_ids.valuation_snapshot)
        self.po.order_line[:1].write({'price_unit': 550, 'product_qty': 120})
        self.assertEqual(claim.purchase_source_status, 'changed')
        self.assertEqual(claim.claim_line_ids.valuation_snapshot, snapshot)
        self.assertEqual(claim.currency_id.compare_amounts(claim.claim_line_ids.provisional_untaxed_amount, 7500), 0)
        self.po.partner_id = self.other_supplier
        claim.invalidate_recordset(['purchase_source_status','purchase_source_warning'])
        self.assertEqual(claim.purchase_source_status, 'changed')
        self.assertEqual(claim.partner_id, self.supplier)
        currency = self.env['res.currency'].with_context(active_test=False).search([('id','!=',self.po.currency_id.id)],limit=1)
        currency.active = True
        self.po.currency_id = currency
        self.assertEqual(claim.currency_id.id, snapshot['currency_id'])
        self.assertEqual(claim.purchase_currency_id, currency)

    def test_source_changes_require_new_review_and_cannot_forge_context_snapshot(self):
        claim = self._claim()
        submit_prepared(claim)
        with self.assertRaises(AccessError):
            claim.write({'review_purchase_context': {'state': 'purchase'}})
        with self.assertRaises(ValidationError):
            claim.write({'purchase_id': self.second.id})
        claim.with_user(self.manager).action_return_to_draft()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.write({'purchase_id': self.second.id, 'claim_line_ids': [Command.update(claim.claim_line_ids.id,
                {'purchase_line_id': self.second.order_line[:1].id})]})
        replacement = self._claim(order=self.second)
        submit_prepared(replacement)
        self.assertEqual(claim.review_purchase_context['purchase_id'], self.po.id)
        self.assertEqual(replacement.review_purchase_context['purchase_id'], self.second.id)
        self.assertEqual(len((claim|replacement).audit_event_ids.filtered(lambda event: event.event_type == 'submitted')), 2)

    def test_search_by_real_products_and_grouped_purchase(self):
        one = self._claim(lines=self.po.order_line[0] | self.po.order_line[2])
        two = self._claim(order=self.second, lines=self.second.order_line[1])
        Claim = self.env['supplier.shortage.claim'].with_user(self.user)
        self.assertEqual(Claim.search([('product_ids','in',self.speaker.ids)]), one)
        self.assertEqual(Claim.search([('product_ids','in',self.charger.ids)]), two)
        self.assertEqual(Claim.search_count([('product_ids','in',(self.speaker|self.cable).ids)]), 1)
        grouped = Claim._read_group([('id','in',(one|two).ids)], ['purchase_id'], ['__count'])
        self.assertEqual({order.id: count for order,count in grouped}, {self.po.id:1,self.second.id:1})

    def test_restrictive_po_line_reference_and_no_source_side_effects(self):
        draft = self._order()
        claim = self._claim(order=draft)
        with mute_logger('odoo.sql_db'), self.assertRaises((IntegrityError,UserError)), self.cr.savepoint():
            draft.order_line[:1].unlink()
        prepared_claim = self._claim()
        prepare_review(prepared_claim)
        before = self.po.read(['partner_id','company_id','currency_id','state','order_line','amount_total'])[0]
        lines = self.po.order_line.read(['product_id','product_qty','uom_id','price_unit','discount','qty_received','qty_invoiced'])
        pickings = self.po.picking_ids.ids
        bills = self.po.invoice_ids.ids
        action = self.po.with_user(self.user).action_create_shortage_claim()
        prepared_claim.action_submit_for_review()
        self.assertEqual(self.po.read(['partner_id','company_id','currency_id','state','order_line','amount_total'])[0],before)
        self.assertEqual(self.po.order_line.read(['product_id','product_qty','uom_id','price_unit','discount','qty_received','qty_invoiced']),lines)
        self.assertEqual(self.po.picking_ids.ids,pickings)
        self.assertEqual(self.po.invoice_ids.ids,bills)

    def test_purchase_and_claim_native_views_and_defaults_load(self):
        unsaved = Form(self.env['purchase.order'].with_user(self.user))
        unsaved.partner_id = self.supplier
        self.assertEqual(unsaved.shortage_claim_count, 0)
        order = self.po.with_user(self.user)
        arch = order.get_view(self.env.ref('purchase.purchase_order_form').id,'form')['arch']
        self.assertIn('action_view_shortage_claims',arch)
        self.assertIn('action_create_shortage_claim',arch)
        claim = self._claim()
        self.assertIn('purchase_source_warning',claim.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_claim_form').id,'form')['arch'])

    def test_forged_computed_source_defaults_cannot_certify_an_rfq(self):
        draft = self._order()
        claim = self.env['supplier.shortage.claim'].with_user(self.user).with_context(
            default_purchase_id=draft.id, default_purchase_state='purchase', default_purchase_source_status='current',
            default_review_purchase_context={'state': 'purchase'}).create({})
        self.assertEqual(claim.purchase_state, 'draft')
        self.assertEqual(claim.purchase_source_status, 'changed')
        self.assertFalse(claim.review_purchase_context)
        with self.assertRaises(ValidationError):
            claim.action_submit_for_review()
