import math
from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .receipt_analysis import _coverage_search
from .receiving_reconciliation import signature


BILL_TYPES = ('in_invoice', 'in_refund')
QUANTITY_KEYS = ('bill_draft', 'bill_posted', 'bill_cancel', 'credit_draft', 'credit_posted', 'credit_cancel')


def empty_billing(note, accessible=True):
    return dict(accessible=accessible, complete=False, note=note, line_ids=[], document_ids=[],
                issues=[], signature=False, **dict.fromkeys(QUANTITY_KEYS, 0))


class PurchaseLineBilling(models.Model):
    _inherit = 'purchase.order.line'

    def _ssc_billing_analysis(self):
        self.check_access('read')
        for order in self.order_id:
            order._check_shortage_claim_source_access()
        AML, Move = self.env['account.move.line'], self.env['account.move']
        try:
            AML.check_access('read')
            Move.check_access('read')
            bad = _coverage_search(self, [('id', 'in', self.ids), ('invoice_lines', 'any!', ['|', '!', ('id', 'access', 'read'), '!', ('move_id', 'access', 'read')])]) if self else self
            if bad:
                raise AccessError(self.env._('Accounting evidence is not fully accessible.'))
            linked = AML.search([('purchase_line_id', 'in', self.ids), ('move_id.move_type', 'in', BILL_TYPES)])
            documents = linked.move_id
            frontier = documents
            while frontier:
                hidden = _coverage_search(frontier, [('id', 'in', frontier.ids), '|',
                    ('reversal_move_ids', 'any!', ['!', ('id', 'access', 'read')]),
                    ('reversed_entry_id', 'any!', ['!', ('id', 'access', 'read')])])
                if hidden:
                    raise AccessError(self.env._('Reversal evidence is not fully accessible.'))
                next_docs = (frontier.reversal_move_ids | frontier.reversed_entry_id).filtered(lambda move: move.move_type in BILL_TYPES) - documents
                next_docs.check_access('read')
                documents |= next_docs
                frontier = next_docs
            hidden = _coverage_search(documents, [('id', 'in', documents.ids), ('invoice_line_ids', 'any!', ['!', ('id', 'access', 'read')])]) if documents else documents
            if hidden:
                raise AccessError(self.env._('Bill lines are not fully accessible.'))
            documents.check_access('read')
            source = documents.invoice_line_ids
            source.check_access('read')
            source.product_id.check_access('read')
            source.product_uom_id.check_access('read')
        except AccessError:
            return {line.id: empty_billing(self.env._('Your permissions do not allow complete vendor billing reconciliation.'), False) for line in self}

        by_purchase = defaultdict(list)
        for row in linked:
            by_purchase[row.purchase_line_id.id].append(row)
        results = {}
        for line in self:
            data = empty_billing(self.env._('No related vendor documents were found.'))
            data['complete'] = bool(line.product_id.type == 'consu' and not line.display_type and not line.is_downpayment)
            rows = AML.browse([row.id for row in by_purchase[line.id]])
            relevant_docs = rows.move_id
            # Header reversal provenance exposes credits, without guessing their line allocation.
            related = relevant_docs
            while related:
                additions = ((related.reversal_move_ids | related.reversed_entry_id) & documents) - relevant_docs
                relevant_docs |= additions
                related = additions
            orphans = relevant_docs.invoice_line_ids.filtered(lambda row: row.display_type == 'product' and not row.purchase_line_id
                and row.product_id and row.product_id.type == 'consu' and not row.is_downpayment)
            evidence = rows | orphans
            data.update(line_ids=evidence.ids, document_ids=relevant_docs.ids)
            for row in rows:
                if row.display_type != 'product' or row.is_downpayment or line.display_type or line.is_downpayment or line.product_id.type != 'consu':
                    continue
                problems = []
                if row.company_id != line.company_id or row.move_id.company_id != line.company_id:
                    problems.append('bill_source_reconciliation_conflict')
                if row.move_id.partner_id.commercial_partner_id != line.order_id.partner_id.commercial_partner_id:
                    problems.append('supplier_mismatch')
                if row.currency_id != line.currency_id:
                    problems.append('currency_mismatch')
                if row.product_id != line.product_id or not row.product_id:
                    problems.append('bill_source_reconciliation_conflict')
                if (not row.product_uom_id or not row.product_uom_id._has_common_reference(line.uom_id)
                        or not math.isfinite(row.quantity) or row.quantity < 0):
                    problems.append('incompatible_bill_uom')
                quantity = None
                if not problems:
                    quantity = row.product_uom_id._compute_quantity(row.quantity, line.uom_id, round=False)
                    if not math.isfinite(quantity) or (row.quantity > 0 and line.uom_id.is_zero(quantity)):
                        problems.append('incompatible_bill_uom')
                if problems:
                    data['complete'] = False
                    data['issues'].extend((category, row.id) for category in problems)
                    continue
                prefix = 'bill' if row.move_id.move_type == 'in_invoice' else 'credit'
                data[prefix + '_' + row.move_id.state] += quantity
            for row in orphans:
                data['issues'].append(('unexpected_credit_note' if row.move_id.move_type == 'in_refund' else 'unmatched_vendor_bill_line', row.id))
                data['complete'] = False
            if not data['complete'] and not data['issues']:
                data['note'] = self.env._('This source purchase line has no supported physical billing basis.')
            if data['issues']:
                data['note'] = self.env._('Vendor document mapping, supplier, currency or UoM requires review. Totals are incomplete; no claim liability is established.')
            elif rows:
                data['note'] = self.env._('Draft and posted quantities are separate. Credits are displayed without automatic netting or recovery allocation.')
            data['signature'] = signature([line.id, line.uom_id.id, line.uom_id.factor, line.currency_id.id,
                line.order_id.partner_id.commercial_partner_id.id, line.product_id.purchase_method, [[row.id, row.purchase_line_id.id, row.company_id.id, row.product_id.id,
                row.product_uom_id.id, row.product_uom_id.factor, row.quantity, row.price_unit, row.discount,
                row.price_subtotal, row.price_total, row.currency_id.id, row.tax_ids.ids,
                row.move_id.state, row.move_id.move_type, row.move_id.partner_id.commercial_partner_id.id,
                row.move_id.ref, row.move_id.invoice_date, row.move_id.date, row.move_id.reversed_entry_id.id]
                for row in evidence.sorted('id')]])
            results[line.id] = data
        return results


class BillLineCompanyIntegrity(models.Model):
    _inherit = 'account.move.line'

    purchase_line_id = fields.Many2one(check_company=True)

    @api.constrains('purchase_line_id', 'company_id', 'move_id')
    def _ssc_check_purchase_company(self):
        for line in self.filtered('purchase_line_id'):
            if line.company_id != line.purchase_line_id.company_id or line.move_id.company_id != line.purchase_line_id.company_id:
                raise ValidationError(self.env._('Vendor bill line and purchase line must belong to the same company.'))
