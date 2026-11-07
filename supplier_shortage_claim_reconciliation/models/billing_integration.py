from collections import defaultdict
from markupsafe import Markup
from psycopg2 import IntegrityError

from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tools import formatLang

from .billing_analysis import QUANTITY_KEYS, empty_billing
from .receiving_reconciliation import signature


LINE_FIELDS = {'billing_basis', 'billing_complete', 'billing_note', 'billing_policy', 'draft_billed_qty', 'posted_billed_qty',
    'canceled_billed_qty', 'draft_credit_qty', 'posted_credit_qty', 'canceled_credit_qty', 'billing_difference_known', 'potential_billed_difference_qty'}
CLAIM_FIELDS = {'billing_summary', 'billing_accessible', 'billing_line_preview_ids', 'billing_document_count',
    'billing_review_warning', 'review_billing_signature', 'billing_exception_ids', 'billing_exception_count'}


class ClaimLineBilling(models.Model):
    _inherit = 'supplier.shortage.claim.line'

    billing_basis = fields.Json(compute='_compute_billing')
    billing_complete = fields.Boolean(compute='_compute_billing')
    billing_note = fields.Char(compute='_compute_billing')
    billing_policy = fields.Char(compute='_compute_billing', string='Native Bill Control Policy')
    draft_billed_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False)
    posted_billed_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False, string='Gross Posted Billed Quantity')
    canceled_billed_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False)
    draft_credit_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False)
    posted_credit_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False)
    canceled_credit_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False)
    billing_difference_known = fields.Boolean(compute='_compute_billing')
    potential_billed_difference_qty = fields.Float(compute='_compute_billing', digits=0, aggregator=False, string='Potential Posted Billed vs Received Difference')

    @api.depends('purchase_line_id', 'company_id', 'claim_id.purchase_id', 'receiving_basis',
        'product_id.purchase_method', 'purchase_line_id.uom_id.factor', 'purchase_line_id.currency_id',
        'purchase_line_id.company_id', 'purchase_line_id.order_id', 'purchase_line_id.order_id.partner_id.commercial_partner_id',
        'purchase_line_id.invoice_lines.product_uom_id.factor', 'purchase_line_id.invoice_lines.product_id',
        'purchase_line_id.invoice_lines.move_id.invoice_line_ids.quantity', 'purchase_line_id.invoice_lines.move_id.invoice_line_ids.product_id',
        'purchase_line_id.invoice_lines.move_id.reversal_move_ids.state', 'purchase_line_id.invoice_lines.move_id.currency_id',
        'purchase_line_id.invoice_lines.move_id.partner_id', 'purchase_line_id.invoice_lines.move_id.ref',
        'purchase_line_id.invoice_lines.move_id.invoice_date', 'purchase_line_id.invoice_lines.price_total',
        'purchase_line_id.invoice_lines.quantity', 'purchase_line_id.invoice_lines.move_id.state', 'purchase_line_id.invoice_lines.price_subtotal')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_billing(self):
        self.check_access('read')
        try:
            data_by_line = self.purchase_line_id._ssc_billing_analysis()
        except AccessError:
            data_by_line = {}
        for line in self:
            data = data_by_line.get(line.purchase_line_id.id, empty_billing(self.env._('Purchase or accounting evidence is unavailable.'), False))
            if data['accessible'] and line.purchase_line_id and (line.purchase_line_id.company_id != line.company_id or line.purchase_line_id.order_id != line.claim_id.purchase_id):
                data = empty_billing(self.env._('Correct the purchase-line company/ownership before reconciling billing.'))
            receipt = line.receiving_basis
            known = data['complete'] and receipt['complete'] and not (data['credit_posted'] or data['credit_draft'])
            difference = data['bill_posted'] - receipt['net'] if known else 0
            line.billing_basis, line.billing_complete, line.billing_note = data, data['complete'], data['note']
            line.billing_policy = dict(line.purchase_line_id.product_id._fields['purchase_method']._description_selection(self.env)).get(line.product_id.purchase_method, '') if data['accessible'] and line.purchase_line_id else ''
            for field, key in zip(('draft_billed_qty','posted_billed_qty','canceled_billed_qty','draft_credit_qty','posted_credit_qty','canceled_credit_qty'), QUANTITY_KEYS):
                line[field] = data[key]
            line.billing_difference_known = bool(known)
            line.potential_billed_difference_qty = difference if known and line.purchase_line_id.uom_id.compare(difference, 0) > 0 else 0

    @api.model_create_multi
    def create(self, vals_list):
        if any(LINE_FIELDS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Billing evidence is read-only and cannot be supplied manually.'))
        lines = super().create(vals_list)
        lines.invalidate_recordset(list(LINE_FIELDS))
        return lines

    def write(self, vals):
        if LINE_FIELDS & vals.keys():
            raise AccessError(self.env._('Billing evidence is read-only.'))
        return super().write(vals)


class ClaimBilling(models.Model):
    _inherit = 'supplier.shortage.claim'

    billing_summary = fields.Html(compute='_compute_billing_context')
    billing_accessible = fields.Boolean(compute='_compute_billing_context')
    billing_line_preview_ids = fields.Many2many('account.move.line', compute='_compute_billing_context')
    billing_document_count = fields.Integer(compute='_compute_billing_context')
    billing_review_warning = fields.Char(compute='_compute_billing_context')
    review_billing_signature = fields.Char(readonly=True, copy=False)
    billing_exception_ids = fields.Many2many('supplier.shortage.receipt.exception', compute='_compute_billing_exceptions')
    billing_exception_count = fields.Integer(compute='_compute_billing_exceptions')

    def _billing_positions(self):
        self.ensure_one()
        self.check_access('read')
        if self.claim_line_ids:
            return {line.purchase_line_id.id or 0: line.billing_basis for line in self._review_lines()}
        if not self.purchase_id:
            return {}
        try:
            return self.purchase_id.order_line.filtered(lambda row: row.product_id.type == 'consu' and not row.display_type and not row.is_downpayment)._ssc_billing_analysis()
        except AccessError:
            return {0: empty_billing(self.env._('Your permissions do not allow complete vendor billing reconciliation.'), False)}

    def _billing_signature(self, positions):
        return signature({str(key): data['signature'] for key,data in positions.items()}) if positions and all(data['accessible'] and data['signature'] for data in positions.values()) else False

    @api.depends('claim_line_ids.billing_basis', 'state', 'purchase_id', 'review_billing_signature')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_billing_context(self):
        self.check_access('read')
        for claim in self:
            positions = claim._billing_positions()
            accessible = all(data['accessible'] for data in positions.values())
            ids = sorted({i for data in positions.values() for i in data['line_ids']}) if accessible else []
            claim.billing_accessible = accessible
            claim.billing_line_preview_ids = self.env['account.move.line'].browse(ids[:40])
            claim.billing_document_count = len({i for data in positions.values() for i in data['document_ids']}) if accessible else 0
            rows = []
            claim_lines = {line.purchase_line_id.id:line for line in claim.claim_line_ids}
            for identifier,data in list(positions.items())[:40]:
                label = self.env['purchase.order.line'].browse(identifier).display_name if identifier and data['accessible'] else self.env._('Source unavailable')
                cells = [formatLang(self.env, data[key], digits=self.env['decimal.precision'].precision_get('Product Unit')) for key in QUANTITY_KEYS] if data['complete'] else [self.env._('Unknown / review')] * 6
                current_line = claim_lines.get(identifier)
                physical = current_line.receiving_basis if current_line else {}
                cells += [formatLang(self.env, physical[key], digits=self.env['decimal.precision'].precision_get('Product Unit')) if physical.get('complete') else self.env._('Unknown / review') for key in ('net','backorder')]
                cells += [formatLang(self.env,current_line.potential_billed_difference_qty,digits=self.env['decimal.precision'].precision_get('Product Unit')) if current_line and current_line.billing_difference_known else self.env._('Unknown / review')]
                cells += [current_line.billing_policy if current_line else '']
                rows.append(Markup('<tr><td>%s</td>%s<td>%s</td></tr>') % (label, Markup('').join(Markup('<td>%s</td>') % cell for cell in cells), data['note']))
            headings = ('PO Line','Draft Bills','Gross Posted Bills','Canceled Bills (Historical)','Draft Credits','Posted Credits','Canceled Credits (Historical)','Net Received','Active Backorders','Potential Difference - Not Confirmed Claim','Bill Control Policy','Information')
            claim.billing_summary = Markup('<div class="table-responsive"><table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>') % (
                Markup('').join(Markup('<th>%s</th>') % self.env._(name) for name in headings), Markup('').join(rows)) if rows else self.env._('Select purchase lines to view vendor billing evidence.')
            warning = False
            if claim.state == 'under_review':
                current = claim._billing_signature(positions)
                if any(not data['accessible'] for data in positions.values()):
                    warning = self.env._('Accounting evidence is inaccessible. Your permissions do not allow complete vendor billing reconciliation.')
                elif any(not data['complete'] for data in positions.values()):
                    warning = self.env._('Billing mapping, company, currency or UoM requires review. Totals are incomplete and cannot establish a claim.')
                elif not current or not claim.review_billing_signature:
                    warning = self.env._('Billing evidence has no complete submitted baseline or is inaccessible. Financial claim eligibility remains unverified.')
                elif current != claim.review_billing_signature:
                    warning = self.env._('Vendor billing changed after submission. Revalidate sources; proposed quantities and valuation snapshots remain unchanged.')
            claim.billing_review_warning = warning

    def _billing_exception_domain(self):
        return [('reconciliation_kind', '=', 'billing'), ('purchase_id', 'in', self.purchase_id.ids), '|',
                ('purchase_line_id', 'in', self.claim_line_ids.purchase_line_id.ids), '|', ('claim_id', 'in', self.ids),
                '&', ('claim_id', '=', False), ('purchase_line_id', '=', False)]

    @api.depends('purchase_id', 'claim_line_ids.purchase_line_id')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_billing_exceptions(self):
        self.check_access('read')
        records = self.env['supplier.shortage.receipt.exception'].search(self._billing_exception_domain())
        for claim in self:
            found = records.filtered(lambda rec: rec.purchase_id == claim.purchase_id and (rec.purchase_line_id in claim.claim_line_ids.purchase_line_id or rec.claim_id == claim or not rec.purchase_line_id and not rec.claim_id))
            claim.billing_exception_ids = found
            claim.billing_exception_count = len(found.filtered(lambda rec: rec.condition_present and rec.review_state in ('new','in_review')))

    def action_view_billing_exceptions(self):
        self.ensure_one()
        self.check_access('read')
        return {'type':'ir.actions.act_window','name':self.env._('Billing Exceptions'),'res_model':'supplier.shortage.receipt.exception',
                'view_mode':'list,form','domain':self._billing_exception_domain(),'context':{'search_default_present':1}}

    def action_view_vendor_bills(self):
        self.ensure_one()
        positions = self._billing_positions()
        if not all(data['accessible'] for data in positions.values()):
            raise AccessError(self.env._('Your permissions do not allow complete vendor billing reconciliation.'))
        ids = sorted({i for data in positions.values() for i in data['document_ids']})
        self.env['account.move'].browse(ids).check_access('read')
        return {'type':'ir.actions.act_window','name':self.env._('Related Vendor Documents'),'res_model':'account.move',
                'view_mode':'list,form','domain':[('id','in',ids)],'context':{'create':False}}

    def action_view_bill_evidence(self):
        self.ensure_one()
        positions = self._billing_positions()
        if not all(data['accessible'] for data in positions.values()):
            raise AccessError(self.env._('Your permissions do not allow complete vendor billing reconciliation.'))
        ids = sorted({i for data in positions.values() for i in data['line_ids']})
        self.env['account.move.line'].browse(ids).check_access('read')
        return {'type':'ir.actions.act_window','name':self.env._('Vendor Billing Evidence'),'res_model':'account.move.line',
            'view_mode':'list,form','domain':[('id','in',ids)],
            'views':[(self.env.ref('supplier_shortage_claim_reconciliation.view_bill_evidence_list').id,'list'),
                     (self.env.ref('supplier_shortage_claim_reconciliation.view_bill_evidence_form').id,'form')],
            'context':{'create':False,'edit':False,'delete':False}}

    def action_refresh_billing_review(self):
        if not any(self.env.user.has_group('supplier_shortage_claim_reconciliation.' + group) for group in ('group_claim_user','group_accounts_reviewer')):
            raise AccessError(self.env._('An authorized purchase/accounts reviewer is required.'))
        self.check_access('read')
        Exception = self.env['supplier.shortage.receipt.exception']
        with self.env.cr.savepoint():
            self.lock_for_update()
            orders = self.purchase_id.sorted('id')
            for order in orders:
                order._check_shortage_claim_source_access()
            orders.lock_for_update()
            if any(not claim.purchase_id or not claim.claim_line_ids for claim in self):
                raise ValidationError(self.env._('Select a purchase order and at least one claim line.'))
            self.claim_line_ids.invalidate_recordset(list(LINE_FIELDS))
            positions = {line.purchase_line_id.id:line.billing_basis for line in self._review_lines()}
            if any(not data['accessible'] for data in positions.values()):
                raise AccessError(self.env._('Your permissions do not allow complete vendor billing reconciliation.'))
            findings = {}
            category_labels = dict(Exception._fields['exception_type']._description_selection(self.env))

            def finding(order, po_line, category, event, rows, data, quantity=None, description=None, stock=None, claim=None, severity='warning'):
                rows.check_access('read')
                key = 'billing/%s/%s/%s/%s/%s' % (order.company_id.id,order.id,po_line.id if po_line else 0,category,event)
                findings[key] = {'reconciliation_kind':'billing','company_id':order.company_id.id,'purchase_id':order.id,
                    'purchase_line_id':po_line.id if po_line else False,'claim_id':claim.id if claim else False,
                    'exception_type':category,'severity':severity,'quantity_known':quantity is not None,
                    'affected_quantity':quantity if quantity is not None else 0,'product_uom_id':po_line.uom_id.id if po_line else False,
                    'source_bill_line_ids':[Command.set(rows.ids)],'source_move_ids':[Command.set(stock.ids if stock else [])],
                    'description':description or data['note'],'source_signature':signature([data.get('signature'),category,event,quantity,rows.ids])}

            for claim in self:
                for line in claim.claim_line_ids:
                    po_line = line.purchase_line_id
                    if not po_line or po_line.order_id != claim.purchase_id or po_line.company_id != claim.company_id:
                        raise ValidationError(self.env._('Correct the purchase-line ownership before billing reconciliation.'))
                    data,receipt = line.billing_basis,line.receiving_basis
                    rows = self.env['account.move.line'].browse(data['line_ids'])
                    for category,row_id in data['issues']:
                        row = rows.browse(row_id)
                        finding(claim.purchase_id,po_line if row.purchase_line_id else po_line.browse(),category,str(row.id),row,data,
                            description=self.env._('Verify vendor line %s: %s. No accounting value or native mapping was changed.',row.display_name,category_labels.get(category,category)),severity='high')
                    if data['complete']:
                        if receipt['complete']:
                            for category,total in [('posted_billed_above_received',data['bill_posted']),('draft_billed_above_received',data['bill_draft'])]:
                                difference = total - receipt['net']
                                if po_line.uom_id.compare(difference,0)>0:
                                    finding(claim.purchase_id,po_line,category,'balance',rows,data,difference,
                                        stock=self.env['stock.move'].browse(receipt['move_ids']), severity='info' if category.startswith('draft') else 'warning',
                                        description=self.env._('Potential billing versus receiving difference only. Ordered-quantity billing may precede delivery; investigate delivery commitments and credits. This is not confirmed supplier liability.'))
                        for row in rows.filtered(lambda row: row.move_id.state == 'cancel'):
                            finding(claim.purchase_id,po_line,'canceled_bill_source_changed',str(row.id),row,data,
                                description=self.env._('Canceled vendor document is historical evidence only and is excluded from effective posted totals.'))
                        if data['credit_posted'] or data['credit_draft']:
                            credits = rows.filtered(lambda row: row.move_id.move_type == 'in_refund' and row.move_id.state != 'cancel')
                            finding(claim.purchase_id,po_line,'unexpected_credit_note','purpose',credits,data,
                                description=self.env._('Credit notes require purpose verification. Quantities are shown separately; no net billing, shortage recovery or settlement allocation was inferred.'))
                current = claim._billing_signature(claim._billing_positions())
                if claim.state == 'under_review' and claim.review_billing_signature and current != claim.review_billing_signature:
                    rows = self.env['account.move.line'].browse(sorted({i for line in claim.claim_line_ids for i in line.billing_basis['line_ids']}))
                    finding(claim.purchase_id,claim.claim_line_ids.purchase_line_id.browse(),'bill_source_reconciliation_conflict','claim%s' % claim.id,rows,
                        {'signature':current,'note':self.env._('Vendor billing changed since submission. Revalidate the claim without rewriting its quantity or valuation snapshot.')},claim=claim)
            # Conservative duplicates: exact nonempty reference/date plus full linked product-line signature.
            rows = self.env['account.move.line'].browse(sorted({i for data in positions.values() for i in data['line_ids']}))
            groups = defaultdict(list)
            for bill in rows.move_id.filtered(lambda move: move.move_type == 'in_invoice' and move.state != 'cancel' and move.ref and move.invoice_date):
                product_rows = bill.invoice_line_ids.filtered(lambda row: row.display_type == 'product')
                if not product_rows or any(not row.purchase_line_id for row in product_rows):
                    continue
                fingerprint = sorted((row.purchase_line_id.id,row.product_id.id,row.quantity,row.product_uom_id.id,row.price_unit,row.discount,tuple(sorted(row.tax_ids.ids))) for row in product_rows)
                groups[(bill.company_id.id,bill.partner_id.commercial_partner_id.id,bill.ref.strip().casefold(),bill.invoice_date,bill.currency_id.id,signature(fingerprint))].append(bill)
            evidence_by_bill = defaultdict(list)
            for row in rows:
                evidence_by_bill[row.move_id.id].append(row)
            order_ids = set(orders.ids)
            for candidates in groups.values():
                if len(candidates) < 2:
                    continue
                candidate_ids = {bill.id for bill in candidates}
                by_order = defaultdict(list)
                for bill in candidates:
                    for row in evidence_by_bill[bill.id]:
                        order_id = row.purchase_line_id.order_id.id
                        if order_id in order_ids:
                            by_order[order_id].append(row.id)
                for order_id, source_ids in by_order.items():
                    support = self.env['account.move.line'].browse(source_ids)
                    finding(orders.browse(order_id),self.env['purchase.order.line'],'potential_duplicate_bill','cluster%s' % min(candidate_ids),support,
                        {'signature':signature([positions[line.id]['signature'] for line in support.purchase_line_id if line.id in positions]),
                         'note':self.env._('Matching supplier invoice reference, date and complete linked line details appear on multiple documents. Accounts must verify this group; no document was canceled.')})
            try:
                Exception._synchronize(self._billing_exception_domain(),findings)
            except IntegrityError as error:
                if error.pgcode != '23505' or not error.diag.constraint_name.endswith('_source_unique'):
                    raise
                raise UserError(self.env._('Billing review changed concurrently or is inaccessible. Verify source permissions and retry in a new transaction.')) from error
            self.invalidate_recordset(['billing_exception_ids','billing_exception_count'])
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if any(CLAIM_FIELDS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Billing context is server-controlled.'))
        records = super().create([dict(values,review_billing_signature=False) for values in vals_list])
        records.invalidate_recordset(list(CLAIM_FIELDS))
        return records

    def write(self, vals):
        if CLAIM_FIELDS & vals.keys():
            raise AccessError(self.env._('Billing context is server-controlled.'))
        return super().write(vals)

    def action_submit_for_review(self):
        with self.env.cr.savepoint():
            result = super().action_submit_for_review()
            for claim in self:
                super(ClaimBilling,claim).write({'review_billing_signature':claim._billing_signature(claim._billing_positions())})
            return result


class BillEvidenceNavigation(models.Model):
    _inherit = 'account.move.line'

    def action_ssc_open_vendor_document(self):
        self.ensure_one()
        self.check_access('read')
        self.move_id.check_access('read')
        if self.move_id.move_type not in ('in_invoice','in_refund'):
            raise ValidationError(self.env._('This is not a vendor bill or vendor credit note.'))
        return {'type':'ir.actions.act_window','name':self.env._('Vendor Document'),'res_model':'account.move',
                'view_mode':'form','views':[(False,'form')],'res_id':self.move_id.id,'context':{'create':False}}
