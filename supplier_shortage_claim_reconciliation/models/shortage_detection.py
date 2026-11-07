import math

from psycopg2 import IntegrityError

from odoo import fields, models, Command
from odoo.fields import Domain
from odoo.tools import float_compare
from odoo.exceptions import AccessError, ValidationError, UserError
from .receiving_reconciliation import signature
from .receipt_analysis import _coverage_search


CLASSIFICATIONS = [
    ('reconciled', 'Reconciled'), ('pending_supplier_delivery', 'Pending Supplier Delivery'),
    ('physical_delivery_difference', 'Physical Delivery Difference'), ('posted_billed_excess', 'Posted Billed Excess'),
    ('shortage_candidate_review', 'Shortage Candidate Review'), ('canceled_delivery_review', 'Canceled Delivery Review'),
    ('over_receipt', 'Over-Receipt'), ('draft_billing_review', 'Draft Billing Review'),
    ('credit_adjustment_review', 'Credit Adjustment Review'), ('source_data_conflict', 'Source Data Conflict'),
    ('insufficient_evidence', 'Insufficient Evidence'), ('unsupported_product_or_uom', 'Unsupported Product / UoM'),
    ('access_incomplete', 'Access Incomplete'),
]


class DetectionReview(models.Model):
    _inherit = 'supplier.shortage.receipt.exception'

    reconciliation_kind = fields.Selection(selection_add=[('detection', 'Shortage Detection')], ondelete={'detection': 'set receiving'})
    exception_type = fields.Selection(selection_add=[('shortage_detection', 'Shortage Detection')], ondelete={'shortage_detection': 'set other_review'})
    classification = fields.Selection(CLASSIFICATIONS, readonly=True, index=True)
    findings = fields.Json(readonly=True)
    findings_summary = fields.Text(readonly=True, string='Additional Findings')
    evidence_complete = fields.Boolean(readonly=True)
    ordered_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    gross_received_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    returned_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    net_received_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    pending_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    pending_backorder_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    posted_billed_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    draft_billed_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    posted_credit_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    physical_difference_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    potential_billed_excess_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    candidate_qty = fields.Float(readonly=True, digits=0, aggregator=False)
    currency_id = fields.Many2one('res.currency', readonly=True)
    indicative_untaxed_exposure = fields.Monetary(readonly=True, aggregator=False, string='Indicative Untaxed Exposure')
    valuation_status = fields.Selection([('review', 'Requires Review'), ('indicative', 'Indicative Estimate'), ('none', 'Not Applicable')], readonly=True)
    priority = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], readonly=True, index=True)
    priority_reason = fields.Text(readonly=True)
    related_claim_ids = fields.Many2many('supplier.shortage.claim', readonly=True)
    claim_overlap = fields.Boolean(readonly=True)
    next_action = fields.Text(readonly=True)

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        return domain & ~Domain('related_claim_ids', 'any!', ~Domain('id', 'access', 'read'))

    def action_run_detection(self):
        self.check_access('write')
        return self.purchase_line_id.action_run_shortage_detection()

    def _transition(self, target, event):
        detections = self.filtered(lambda rec: rec.reconciliation_kind == 'detection')
        if detections and target == 'resolved':
            before = {rec.id: rec.source_signature for rec in detections}
            detections.purchase_line_id.action_run_shortage_detection()
            if any(rec.source_signature != before[rec.id] or not rec.evidence_complete
                   or rec.classification != 'shortage_candidate_review' or rec.candidate_qty <= 0 for rec in detections):
                raise ValidationError(self.env._('Refresh detection and review the current complete candidate evidence before verification.'))
        return super()._transition(target, event)

    def action_verify_candidate(self):
        if any(rec.classification != 'shortage_candidate_review' or not rec.evidence_complete or rec.candidate_qty <= 0 for rec in self):
            raise AccessError(self.env._('Only a complete shortage review candidate can be verified. Refresh and investigate first.'))
        # Resolved means evidence reviewed, never claim approval or supplier acceptance.
        return self.action_resolve()


class DetectionPurchase(models.Model):
    _inherit = 'purchase.order'

    def action_run_shortage_detection(self):
        self.check_access('read')
        return self.order_line.filtered(lambda line: not line.display_type).action_run_shortage_detection()


class DetectionPurchaseLine(models.Model):
    _inherit = 'purchase.order.line'

    def action_run_shortage_detection(self):
        try:
            return self._ssc_run_detection()
        except IntegrityError as error:
            if error.pgcode != '23505' or not error.diag.constraint_name.endswith('_source_unique'):
                raise
            raise UserError(self.env._('Detection changed concurrently or an existing review is inaccessible. Check permissions and retry in a new transaction.')) from error

    def _ssc_detection_source_signature(self, receiving, billing, integrity):
        self.ensure_one()
        return signature([receiving['signature'], billing['signature'], integrity.get('signature'),
            receiving['complete'], billing['complete'], receiving['accessible'], billing['accessible'],
            self.company_id.id, self.order_id.partner_id.id, self.order_id.state,
            self.product_id.id, self.uom_id.id, self.uom_id.factor, self.currency_id.id,
            self.product_qty, self.price_unit, self.discount, self.tax_ids.ids, self.document_tax_mode,
            self.currency_id.rounding, self.company_id.tax_calculation_rounding_method,
            [[tax.id,tax.amount_type,tax.amount,tax.sequence,tax.include_base_amount,tax.is_base_affected,
              tax.with_company(self.company_id)._is_price_included(document_tax_mode=self.document_tax_mode)]
             for tax in self.tax_ids.flatten_taxes_hierarchy()]])

    def _ssc_run_detection(self):
        if not self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_user'):
            raise AccessError(self.env._('A purchase claim user is required to run detection.'))
        self.check_access('read')
        self = self.filtered(lambda line: not line.display_type)
        orders = self.order_id.sorted('id')
        for order in orders:
            order._check_shortage_claim_source_access()
        with self.env.cr.savepoint():
            orders.lock_for_update()
            self.invalidate_recordset()
            receiving = self._ssc_receiving_analysis()
            billing = self._ssc_billing_analysis()
            ClaimLine = self.env['supplier.shortage.claim.line']
            ClaimLine.check_access('read')
            hidden_claims = _coverage_search(orders, [('id', 'in', orders.ids), ('shortage_claim_ids', 'any!', ['|', '!', ('id', 'access', 'read'), ('claim_line_ids', 'any!', ['!', ('id', 'access', 'read')])])])
            if hidden_claims:
                raise AccessError(self.env._('Related claim evidence is incomplete.'))
            claims = ClaimLine.search([('purchase_line_id', 'in', self.ids)])
            findings = {}
            for line in self:
                r, b = receiving[line.id], billing[line.id]
                integrity = line._ssc_receiving_integrity(r) if r['accessible'] else {}
                complete = r['complete'] and b['complete'] and not integrity.get('native_conflict', False)
                positive = lambda value: line.uom_id.compare(value, 0) > 0
                physical = max(line.product_qty-r['net'], 0) if r['complete'] else 0
                excess = max(b['bill_posted']-r['net'], 0) if complete else 0
                flags = []
                if positive(r['pending']): flags.append('pending_supplier_delivery')
                if positive(physical): flags.append('physical_delivery_difference')
                if positive(excess): flags.append('posted_billed_excess')
                if positive(b['bill_draft']): flags.append('draft_billing_review')
                if positive(b['credit_posted']) or positive(r['returned']): flags.append('credit_adjustment_review')
                if positive(r['cancelled']): flags.append('canceled_delivery_review')
                if b['complete'] and positive(b['bill_posted']-line.product_qty): flags.append('overbilling_above_order')
                linked = claims.filtered(lambda row: row.purchase_line_id == line).claim_id
                if linked: flags.append('existing_claim_overlap')
                if not r['accessible'] or not b['accessible']: category = 'access_incomplete'
                elif line.product_id.type != 'consu' or line.qty_received_method != 'stock_moves': category = 'unsupported_product_or_uom'
                elif not complete: category = 'source_data_conflict'
                elif line.order_id.state == 'cancel': category = 'canceled_delivery_review'
                elif line.order_id.state not in ('purchase','done'): category = 'insufficient_evidence'
                elif 'credit_adjustment_review' in flags: category = 'credit_adjustment_review'
                elif positive(r['net']-line.product_qty): category = 'over_receipt'
                elif positive(r['pending']) and positive(physical): category = 'pending_supplier_delivery'
                elif positive(physical) and positive(excess): category = 'shortage_candidate_review'
                elif positive(physical): category = 'physical_delivery_difference'
                elif positive(excess): category = 'posted_billed_excess'
                elif positive(b['bill_draft']): category = 'draft_billing_review'
                else: category = 'reconciled'
                candidate = min(physical, excess) if category == 'shortage_candidate_review' else 0
                rows = self.env['account.move.line'].browse(b['line_ids'])
                posted = rows.filtered(lambda row: row.move_id.state == 'posted' and row.move_id.move_type == 'in_invoice')
                duplicate_keys = []
                for bill in rows.move_id.filtered(lambda doc: doc.move_type == 'in_invoice' and doc.state != 'cancel' and doc.ref and doc.invoice_date):
                    product_rows = bill.invoice_line_ids.filtered(lambda row: row.display_type == 'product')
                    duplicate_keys.append(signature([bill.company_id.id, bill.partner_id.commercial_partner_id.id,
                        bill.ref.strip().casefold(), bill.invoice_date, bill.currency_id.id,
                        sorted((row.purchase_line_id.id, row.product_id.id, row.quantity, row.product_uom_id.id,
                                row.price_unit, row.discount, sorted(row.tax_ids.ids)) for row in product_rows)]))
                if len(duplicate_keys) != len(set(duplicate_keys)):
                    flags.append('potential_duplicate_bill')
                    category, candidate = 'source_data_conflict', 0
                # An exact common normalized price is required; ambiguous allocation has no estimate.
                prices = []
                for row in (posted if candidate else self.env['account.move.line']):
                    qty = row.product_uom_id._compute_quantity(row.quantity, line.uom_id, round=False)
                    if positive(qty): prices.append(row.price_subtotal/qty)
                valuation, amount = ('review' if candidate or positive(excess) or not complete or 'credit_adjustment_review' in flags else 'none'), 0
                purchase_price = line.price_unit * (1-line.discount/100)
                price_digits = max(6, self.env['decimal.precision'].precision_get('Product Price'))
                equal_price = lambda first, second: float_compare(first,second,precision_digits=price_digits) == 0
                safe_price = (prices and math.isfinite(purchase_price) and purchase_price >= 0
                    and 0 <= line.discount <= 100 and all(math.isfinite(price) and price >= 0 for price in prices)
                    and all(equal_price(price,prices[0]) for price in prices))
                if candidate and safe_price and equal_price(prices[0],purchase_price) and not linked and not line.tax_ids and not posted.tax_ids:
                    valuation, amount = 'indicative', line.currency_id.round(candidate*prices[0])
                elif candidate:
                    flags.append('valuation_requires_review')
                priority = 'high' if category in ('source_data_conflict','access_incomplete','shortage_candidate_review') or 'overbilling_above_order' in flags else ('low' if category == 'reconciled' else 'medium')
                priority_reason = 'Evidence/category review priority; no supplier liability is established.'
                if valuation == 'indicative' and line.currency_id == line.company_id.currency_id:
                    if line.company_id.ssc_detection_critical_threshold > 0 and amount >= line.company_id.ssc_detection_critical_threshold:
                        priority = 'critical'
                    elif line.company_id.ssc_detection_high_threshold > 0 and amount >= line.company_id.ssc_detection_high_threshold:
                        priority = 'high'
                    priority_reason += ' Reliable company-currency indicative exposure evaluated against company thresholds.'
                finding_labels = dict(CLASSIFICATIONS, existing_claim_overlap='Existing Claim Allocation Review',
                    overbilling_above_order='Posted Billing Above Ordered Quantity',
                    potential_duplicate_bill='Potential Duplicate Vendor Bill',
                    valuation_requires_review='Valuation Basis Requires Review')
                values = dict(company_id=line.company_id.id, purchase_id=line.order_id.id, purchase_line_id=line.id,
                    reconciliation_kind='detection', exception_type='shortage_detection', classification=category,
                    severity='high' if priority == 'high' else 'info', evidence_complete=complete, findings=flags, findings_summary='\n'.join(finding_labels.get(flag,flag) for flag in flags),
                    quantity_known=complete, affected_quantity=candidate, product_uom_id=line.uom_id.id,
                    ordered_qty=line.product_qty, gross_received_qty=r['gross'], returned_qty=r['returned'], net_received_qty=r['net'],
                    pending_qty=r['pending'], pending_backorder_qty=r['backorder'], posted_billed_qty=b['bill_posted'],
                    draft_billed_qty=b['bill_draft'], posted_credit_qty=b['credit_posted'], physical_difference_qty=physical,
                    potential_billed_excess_qty=excess, candidate_qty=candidate, currency_id=line.currency_id.id,
                    indicative_untaxed_exposure=amount, valuation_status=valuation, priority=priority, priority_reason=priority_reason,
                    related_claim_ids=[Command.set(linked.ids)], claim_overlap=bool(linked),
                    source_move_ids=[Command.set(r['move_ids'])], source_bill_line_ids=[Command.set(b['line_ids'])],
                    next_action='Review delivery commitments, warehouse evidence, bill provenance, credits and existing claim allocations. Verify evidence only; no formal claim is created.',
                    description='Detection snapshot: %s. Findings: %s. %s %s Indicative exposure is not verified recovery.' % (category, ', '.join(flags), r['note'], b['note']))
                values['source_signature'] = line._ssc_detection_source_signature(r, b, integrity)
                findings['%s/%s/detection' % (line.company_id.id,line.id)] = values
            scope = [('reconciliation_kind','=','detection'),('purchase_line_id','in',self.ids)]
            records = self.env['supplier.shortage.receipt.exception']._synchronize(scope, findings)
        return {'type':'ir.actions.act_window','name':self.env._('Shortage Detection'), 'res_model':records._name,
                'view_mode':'list,form','domain':[('id','in',records.ids)],
                'views':[(self.env.ref('supplier_shortage_claim_reconciliation.view_detection_list').id,'list'),
                         (self.env.ref('supplier_shortage_claim_reconciliation.view_detection_form').id,'form')]}




class DetectionCompany(models.Model):
    _inherit = 'res.company'

    ssc_detection_high_threshold = fields.Monetary(string='High Detection Review Exposure', default=10000, currency_field='currency_id')
    ssc_detection_critical_threshold = fields.Monetary(string='Critical Detection Review Exposure', default=100000, currency_field='currency_id')
