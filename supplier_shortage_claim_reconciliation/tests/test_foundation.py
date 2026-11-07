from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, new_test_user


class TestFoundation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({'name': 'SSC Other'})
        cls.supplier = cls.env['res.partner'].create({'name': 'SSC Supplier'})
        cls.product = cls.env['product.product'].create({'name': 'SSC Product'})
        cls.user = new_test_user(cls.env, login='ssc_preparer',
            groups='supplier_shortage_claim_reconciliation.group_claim_user,purchase.group_purchase_user',
            company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.order = cls.env['purchase.order'].create({'partner_id': cls.supplier.id,
            'company_id': cls.company.id, 'order_line': [Command.create({
                'product_id': cls.product.id, 'product_qty': 2, 'price_unit': 10})]})

    def claim_values(self):
        return {'partner_id': self.supplier.id, 'purchase_id': self.order.id}

    def test_registry(self):
        for model in ('supplier.shortage.claim', 'supplier.shortage.claim.line'):
            self.assertIn(model, self.env.registry)

    def test_creation_defaults_and_reference(self):
        model = self.env['supplier.shortage.claim'].with_user(self.user)
        first, second = model.create([self.claim_values(), self.claim_values()])
        self.assertEqual(first.company_id, self.company)
        self.assertEqual(first.currency_id, self.company.currency_id)
        self.assertEqual(first.user_id, self.user)
        self.assertEqual(first.state, 'draft')
        self.assertTrue(first.name.startswith('SSC/'))
        self.assertNotEqual(first.name, second.name)
        with self.assertRaises(UserError):
            first.write({'name': 'tampered'})

    def test_line_references(self):
        claim = self.env['supplier.shortage.claim'].with_user(self.user).create(self.claim_values())
        line = self.env['supplier.shortage.claim.line'].with_user(self.user).create({
            'claim_id': claim.id, 'purchase_line_id': self.order.order_line.id,
            'product_id': self.product.id, 'proposed_qty': 1})
        self.assertEqual(claim.claim_line_ids, line)
        self.assertEqual(line.company_id, claim.company_id)
        self.assertEqual(line.currency_id, claim.currency_id)
        with self.assertRaises(UserError), self.cr.savepoint():
            line.write({'product_id': False})
        with self.assertRaises(UserError), self.cr.savepoint():
            claim.write({'purchase_id': False})

    def test_cross_company_relations(self):
        supplier = self.env['res.partner'].create({'name': 'Private supplier', 'company_id': self.other_company.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['supplier.shortage.claim'].create({'partner_id': supplier.id, 'company_id': self.company.id})
        order = self.env['purchase.order'].with_company(self.other_company).create({'partner_id': self.supplier.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['supplier.shortage.claim'].create({'partner_id': self.supplier.id,
                'company_id': self.company.id, 'purchase_id': order.id})

    def test_cross_company_product_and_line(self):
        claim = self.env['supplier.shortage.claim'].create(self.claim_values())
        product = self.env['product.product'].create({'name': 'Private product', 'company_id': self.other_company.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['supplier.shortage.claim.line'].create({'claim_id': claim.id, 'product_id': product.id, 'proposed_qty': 1})
        other_order = self.env['purchase.order'].with_company(self.other_company).create({
            'partner_id': self.supplier.id, 'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 1, 'price_unit': 1})]})
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['supplier.shortage.claim.line'].create({'claim_id': claim.id,
                'purchase_line_id': other_order.order_line.id, 'product_id': self.product.id, 'proposed_qty': 1})

    def test_company_change_rechecks_lines(self):
        product = self.env['product.product'].create({'name': 'SSC Company Product', 'company_id': self.company.id})
        claim = self.env['supplier.shortage.claim'].create({'partner_id': self.supplier.id,
            'claim_line_ids': [Command.create({'product_id': product.id, 'proposed_qty': 1})]})
        with self.assertRaises(UserError), self.cr.savepoint():
            claim.write({'company_id': self.other_company.id, 'user_id': False})
