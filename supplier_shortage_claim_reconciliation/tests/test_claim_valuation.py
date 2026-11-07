from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from copy import deepcopy

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools.float_utils import float_compare


class TestClaimValuation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.precision = cls.env['decimal.precision'].search([('name', '=', 'Product Unit')])
        cls.precision.digits = 3
        cls.usd = cls.env.ref('base.USD')
        cls.inr = cls.env.ref('base.INR')
        (cls.usd | cls.inr).write({'active': True})
        cls.company = cls.env['res.company'].create({'name': 'SSC Valuation INR', 'currency_id': cls.inr.id, 'country_id': cls.env.ref('base.in').id})
        cls.other = cls.env['res.company'].create({'name': 'SSC Valuation Other'})
        cls.units = cls.env.ref('uom.product_uom_unit')
        cls.box = cls.env['uom.uom'].create({'name': 'SSC Box of 12', 'relative_uom_id': cls.units.id, 'relative_factor': 12})
        cls.unrelated = cls.env['uom.uom'].create({'name': 'SSC Unrelated Measure'})
        cls.supplier = cls.env['res.partner'].create({'name': 'SSC Valuation Shared Supplier'})
        cls.product = cls.env['product.product'].create({'name': 'SSC Valuation Shared Product', 'uom_id': cls.units.id})
        cls.user = new_test_user(cls.env, login='ssc_value_user',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.manager = new_test_user(cls.env, login='ssc_value_manager',
            groups='supplier_shortage_claim_reconciliation.group_claim_manager,purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.viewer = new_test_user(cls.env, login='ssc_value_viewer',
            groups='supplier_shortage_claim_reconciliation.group_warehouse_user,purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.tax_group = cls.env['account.tax.group'].create({'name': 'SSC Test Tax Group', 'company_id': cls.company.id})
        cls.po = cls._order()

    @classmethod
    def _order(cls, price=500, discount=0, uom=None, taxes=None, mode='tax_excluded', currency=None):
        return cls.env['purchase.order'].with_company(cls.company).create({
            'partner_id': cls.supplier.id, 'company_id': cls.company.id, 'currency_id': (currency or cls.usd).id,
            'document_tax_mode': mode, 'state': 'purchase', 'order_line': [Command.create({'product_id': cls.product.id,
                'product_qty': 10, 'uom_id': (uom or cls.units).id, 'price_unit': price,
                'discount': discount, 'tax_ids': [Command.set((taxes or cls.env['account.tax']).ids)]})]})

    def _claim(self, quantity=15, uom=None, po=None):
        po = po or self.po
        values = {'purchase_line_id': po.order_line.id, 'proposed_qty': quantity}
        if uom:
            values['claim_uom_id'] = uom.id
        return self.env['supplier.shortage.claim'].with_user(self.user).create({
            'company_id': self.company.id, 'partner_id': self.supplier.id, 'purchase_id': po.id,
            'claim_line_ids': [Command.create(values)]})

    def _tax(self, amount_type='percent', amount=10, included=False):
        return self.env['account.tax'].with_company(self.company).create({
            'name': 'SSC Valuation Tax', 'company_id': self.company.id, 'type_tax_use': 'purchase',
            'amount_type': amount_type, 'amount': amount, 'tax_group_id': self.tax_group.id,
            'price_include_override': 'tax_included' if included else 'tax_excluded'})

    def assert_money(self, value, expected, currency=None):
        self.assertEqual((currency or self.usd).compare_amounts(value, expected), 0)

    def assert_qty(self, value, expected, digits=8):
        self.assertEqual(float_compare(value, expected, precision_digits=digits), 0)

    def test_standard_price_and_integer_estimate(self):
        line = self._claim().claim_line_ids
        self.assertEqual(line.valuation_status, 'estimated')
        self.assert_qty(line.purchase_qty, 10)
        self.assert_qty(line.normalized_purchase_qty, 15)
        self.assert_money(line.source_unit_price, 500)
        self.assert_money(line.provisional_unit_price, 500)
        self.assert_money(line.provisional_untaxed_amount, 7500)
        self.assertFalse(line.product_id.company_id)
        self.assertFalse(line.claim_id.partner_id.company_id)
        self.assertFalse(line.valuation_snapshot)

    def test_fractional_quantities_follow_configured_precision(self):
        for quantity in (1, 1.5, 0.25, 0.333):
            with self.subTest(quantity=quantity):
                line = self._claim(quantity).claim_line_ids
                self.assert_qty(line.normalized_purchase_qty, quantity)
                self.assert_money(line.provisional_untaxed_amount, quantity * 500)

    def test_zero_negative_nonfinite_and_effectively_zero(self):
        for quantity in (0, -1, 0.0000001, float('inf'), float('nan')):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError), self.cr.savepoint():
                self._claim(quantity)

    def test_precision_changes_and_no_silent_input_rounding(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._claim(0.3333)
        self.precision.digits = 8
        line = self._claim(0.0000001).claim_line_ids
        self.assert_qty(line.normalized_purchase_qty, 0.0000001)
        self.precision.digits = 0
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._claim(1.5)

    def test_incompatible_uom_rejected_on_create_and_write(self):
        with self.assertRaisesRegex(ValidationError, 'reference unit'), self.cr.savepoint():
            self._claim(1, self.unrelated)
        line = self._claim().claim_line_ids
        with self.assertRaises(ValidationError), self.cr.savepoint():
            line.write({'claim_uom_id': self.unrelated.id})

    def test_ten_boxes_to_120_units(self):
        line = self._claim(10, self.box).claim_line_ids
        self.assert_qty(line.proposed_qty, 10)
        self.assertEqual(line.claim_uom_id, self.box)
        self.assert_qty(line.normalized_purchase_qty, 120)
        self.assert_money(line.provisional_untaxed_amount, 60000)

    def test_six_units_to_half_box_and_valuation(self):
        po = self._order(price=2400, uom=self.box)
        line = self._claim(6, self.units, po).claim_line_ids
        self.assertEqual(line.product_uom_id, self.box)
        self.assert_qty(line.normalized_purchase_qty, 0.5)
        self.assert_money(line.provisional_untaxed_amount, 1200)

    def test_conversion_does_not_round_up_fractional_boxes(self):
        po = self._order(price=2400, uom=self.box)
        line = self._claim(1, self.units, po).claim_line_ids
        self.assert_qty(line.normalized_purchase_qty, 1 / 12)
        self.assert_money(line.provisional_untaxed_amount, 200)

    def test_discount_and_tax_exclusive_estimate(self):
        tax = self._tax()
        po = self._order(price=500, discount=10, taxes=tax)
        line = self._claim(15, po=po).claim_line_ids
        self.assert_qty(line.source_discount, 10)
        self.assert_money(line.provisional_unit_price, 450)
        self.assert_money(line.provisional_untaxed_amount, 6750)

    def test_tax_inclusive_document_mode_and_discount(self):
        tax = self._tax(included=True)
        po = self._order(price=550, discount=10, taxes=tax, mode='tax_included')
        line = self._claim(2, po=po).claim_line_ids
        self.assertEqual(line.source_tax_mode, 'tax_included')
        self.assert_money(line.provisional_unit_price, 450)
        self.assert_money(line.provisional_untaxed_amount, 900)

    def test_document_mode_controls_price_tax_basis(self):
        tax = self._tax()
        tax.price_include_override = False
        po = self._order(price=500, taxes=tax, mode='tax_excluded')
        line = self._claim(2, po=po).claim_line_ids
        self.assert_money(line.provisional_untaxed_amount, 1000)
        po.document_tax_mode = 'tax_included'
        self.assert_money(line.provisional_untaxed_amount, 909.09)
        submit_prepared(line.claim_id)
        self.assertTrue(line.valuation_snapshot['taxes'][0]['price_include'])
        line.claim_id.with_user(self.manager).action_return_to_draft()
        # Native Odoo gives an explicit tax override priority over document mode.
        tax.price_include_override = 'tax_excluded'
        self.assert_money(line.provisional_untaxed_amount, 1000)

    def test_fixed_tax_included_uses_proposed_quantity(self):
        tax = self._tax(amount_type='fixed', amount=5, included=True)
        po = self._order(price=500, taxes=tax, mode='tax_included')
        self.assert_money(self._claim(2, po=po).claim_line_ids.provisional_untaxed_amount, 990)

    def test_rounding_happens_after_precise_unit_valuation(self):
        po = self._order(price=0.33333)
        line = self._claim(3, po=po).claim_line_ids
        self.assert_qty(line.source_unit_price, 0.33333)
        self.assert_qty(line.provisional_unit_price, 0.33333)
        self.assert_money(line.provisional_untaxed_amount, 1)

    def test_usd_transaction_currency_is_not_company_inr(self):
        line = self._claim(0.25).claim_line_ids
        self.assertEqual(line.currency_id, self.usd)
        self.assertEqual(line.company_id.currency_id, self.inr)
        self.assert_money(line.provisional_untaxed_amount, 125)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            line.claim_id.write({'currency_id': self.inr.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            line.write({'currency_id': self.inr.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['supplier.shortage.claim'].with_user(self.user).create({
                'company_id': self.company.id, 'partner_id': self.supplier.id,
                'purchase_id': self.po.id, 'currency_id': self.inr.id})

    def test_financial_outputs_and_snapshot_cannot_be_forged(self):
        claim = self._claim()
        line = claim.claim_line_ids
        for field, value in (('source_unit_price', 1), ('source_discount', 100), ('provisional_unit_price', 1),
                             ('provisional_untaxed_amount', 1), ('normalized_purchase_qty', 999),
                             ('valuation_snapshot', {'provisional_untaxed_amount': 999}), ('review_purchase_uom_id', self.box.id), ('valuation_status', 'estimated')):
            with self.subTest(field=field), self.assertRaises(ValidationError), self.cr.savepoint():
                line.write({field: value})
        with self.assertRaises(AccessError), self.cr.savepoint():
            claim.write({'review_currency_id': self.inr.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env[line._name].with_user(self.user).create({'claim_id': claim.id,
                'purchase_line_id': self.po.order_line.id, 'proposed_qty': 1, 'source_unit_price': 1})
        injected = self.env[line._name].with_user(self.user).with_context(default_valuation_snapshot={'provisional_untaxed_amount': 999},
            default_provisional_untaxed_amount=999).create({'claim_id': claim.id, 'purchase_line_id': self.po.order_line.id, 'proposed_qty': 1})
        self.assertFalse(injected.valuation_snapshot)
        self.assert_money(injected.provisional_untaxed_amount, 500)
        with self.assertRaises(AccessError), self.cr.savepoint():
            line.with_user(self.viewer).write({'proposed_qty': 2})

    def test_review_freezes_prices_taxes_uom_factors_and_currency(self):
        claim = self._claim(6, self.units, self._order(price=2400, uom=self.box))
        line = claim.claim_line_ids
        submit_prepared(claim)
        snapshot = deepcopy(line.valuation_snapshot)
        self.assert_money(line.provisional_untaxed_amount, 1200)
        source = line.purchase_line_id.with_user(self.env.user)
        source.write({'price_unit': 4800, 'discount': 10})
        source.order_id.write({'currency_id': self.inr.id})
        source.write({'uom_id': self.units.id})
        self.assertEqual(line.review_purchase_uom_id, self.box)
        self.assertEqual(line.product_uom_id, self.box)
        self.box.relative_factor = 24
        self.assert_money(line.provisional_untaxed_amount, 1200)
        self.assert_qty(line.normalized_purchase_qty, 0.5)
        self.assert_money(line.source_unit_price, 2400)
        self.assertEqual(claim.currency_id, self.usd)
        self.assertEqual(line.valuation_snapshot, snapshot)
        self.assertEqual(line.snapshot_currency_id, self.usd)
        self.assertTrue(line.valuation_snapshot_at)
        for values in ({'proposed_qty': 7}, {'claim_uom_id': self.box.id}):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                line.write(values)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            claim.action_refresh_provisional_valuation()

    def test_return_refresh_correct_and_resubmit_preserves_prior_basis(self):
        claim = self._claim(2)
        submit_prepared(claim)
        old = deepcopy(claim.claim_line_ids.valuation_snapshot)
        self.po.order_line.price_unit = 600
        claim.with_user(self.manager).action_return_to_draft()
        claim.action_refresh_provisional_valuation()
        self.assertEqual(claim.claim_line_ids.valuation_snapshot, old)
        self.assert_money(claim.claim_line_ids.submitted_untaxed_amount, 1000)
        self.assert_money(claim.claim_line_ids.provisional_untaxed_amount, 1200)
        prepare_review(claim)
        claim.claim_line_ids.write({'proposed_qty': 3})
        submit_prepared(claim)
        self.assert_money(claim.claim_line_ids.provisional_untaxed_amount, 1800)
        messages = claim.audit_event_ids
        self.assertEqual(len(messages.filtered(lambda event: event.event_type == 'submitted' and event.valuation_basis)), 2)
        with self.assertRaises(AccessError), self.cr.savepoint():
            claim.with_user(self.viewer).action_refresh_provisional_valuation()

    def test_negative_source_pricing_is_marked_for_review(self):
        po = self._order(price=-5)
        line = self._claim(1, po=po).claim_line_ids
        self.assertEqual(line.valuation_status, 'needs_review')
        self.assertTrue(line.valuation_note)
        submit_prepared(line.claim_id)
        self.assertEqual(line.submitted_valuation_status, 'needs_review')

    def test_cross_company_financial_linkage_rejected(self):
        foreign_po = self.env['purchase.order'].with_company(self.other).create({'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 1, 'price_unit': 1})]})
        line = self._claim(1).claim_line_ids
        with self.assertRaises(AccessError), self.cr.savepoint():
            line.write({'purchase_line_id': foreign_po.order_line.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            line.write({'company_id': self.other.id})

    def test_batch_claim_lines_snapshot_without_currency_sum(self):
        original_pickings = self.po.picking_ids
        claim = self._claim(2)
        self.env['supplier.shortage.claim.line'].with_user(self.user).create([
            {'claim_id': claim.id, 'purchase_line_id': self.po.order_line.id, 'proposed_qty': quantity} for quantity in (1, 3)])
        submit_prepared(claim)
        self.assertEqual(len(claim.claim_line_ids), 3)
        for line in claim.claim_line_ids:
            self.assertEqual(line.valuation_status, 'estimated')
            self.assert_money(line.provisional_untaxed_amount, line.proposed_qty * 500)
        self.assertFalse(self.env['account.move'].search([('invoice_origin', '=', self.po.name)]))
        self.assertEqual(self.po.picking_ids, original_pickings)
        self.assertNotIn('recovered_amount', claim._fields)
        self.assertNotIn('verified_shortage_qty', claim.claim_line_ids._fields)

    def test_native_form_exposes_quantity_and_currency_valuation(self):
        model = self.env['supplier.shortage.claim'].with_user(self.user)
        with Form(model) as form:
            form.partner_id = self.supplier
            form.purchase_id = self.po
            with form.claim_line_ids.new() as line:
                line.purchase_line_id = self.po.order_line
                line.claim_uom_id = self.box
                line.proposed_qty = 1
        self.assert_money(form.record.claim_line_ids.provisional_untaxed_amount, 6000)

    def test_draft_purchase_uom_change_preserves_entered_physical_unit(self):
        claim = self._claim(6)
        line = claim.claim_line_ids
        self.assertEqual(line.claim_uom_id, self.units)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            line.write({'claim_uom_id': False})
        self.po.order_line.write({'uom_id': self.box.id, 'price_unit': 2400})
        self.assertEqual(line.claim_uom_id, self.units)
        self.assert_qty(line.normalized_purchase_qty, 0.5)
        self.assert_money(line.provisional_untaxed_amount, 1200)
