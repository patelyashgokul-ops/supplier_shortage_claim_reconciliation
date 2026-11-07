from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user


class TestSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other = cls.env['res.company'].create({'name': 'SSC Restricted'})
        cls.partner = cls.env['res.partner'].create({'name': 'SSC Security Supplier'})
        cls.claim = cls.env['supplier.shortage.claim'].create({'partner_id': cls.partner.id})
        cls.line = cls.env['supplier.shortage.claim.line'].create({'claim_id': cls.claim.id, 'proposed_qty': 1})
        cls.foreign = cls.env['supplier.shortage.claim'].with_company(cls.other).create({'partner_id': cls.partner.id, 'user_id': False})
        cls.foreign_line = cls.env['supplier.shortage.claim.line'].create({'claim_id': cls.foreign.id, 'proposed_qty': 1})
        cls.users = {}
        for role in ('warehouse_user', 'claim_user', 'claim_manager', 'accounts_reviewer', 'finance_manager', 'module_admin', 'unrelated'):
            group = 'base.group_user' if role == 'unrelated' else 'supplier_shortage_claim_reconciliation.group_' + role
            cls.users[role] = new_test_user(cls.env, login='ssc_' + role, groups=group,
                company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])

    def test_role_permissions(self):
        for role, user in self.users.items():
            for record in (self.claim, self.line):
                current = record.with_user(user)
                if role == 'unrelated':
                    with self.assertRaises(AccessError):
                        current.read(['note'])
                else:
                    current.read(['note'])
                values = {'partner_id': self.partner.id} if record == self.claim else {'claim_id': self.claim.id, 'proposed_qty': 1}
                if role in ('claim_user', 'claim_manager'):
                    current.write({'note': 'Prepared'})
                    self.env[record._name].with_user(user).create(values)
                else:
                    with self.assertRaises(AccessError), self.cr.savepoint():
                        current.write({'note': 'Unauthorized'})
                    with self.assertRaises(AccessError), self.cr.savepoint():
                        self.env[record._name].with_user(user).create(values)
                with self.assertRaises(AccessError), self.cr.savepoint():
                    current.unlink()

    def test_company_isolation(self):
        for role, user in self.users.items():
            if role == 'unrelated':
                continue
            for record in (self.foreign, self.foreign_line):
                current = record.with_user(user)
                with self.assertRaises(AccessError):
                    current.read(['note'])
                self.assertFalse(self.env[record._name].with_user(user).search([('id', '=', record.id)]))
                with self.assertRaises(AccessError), self.cr.savepoint():
                    current.write({'note': 'Unauthorized'})
        user = self.users['claim_user']
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['supplier.shortage.claim'].with_user(user).create({'partner_id': self.partner.id, 'company_id': self.other.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['supplier.shortage.claim.line'].with_user(user).create({'claim_id': self.foreign.id, 'proposed_qty': 1})
