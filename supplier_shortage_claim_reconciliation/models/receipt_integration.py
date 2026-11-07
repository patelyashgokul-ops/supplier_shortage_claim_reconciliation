import hashlib
import json

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import formatLang

from .receipt_analysis import RECEIVING_STATES, empty_position


LINE_RECEIPT_FIELDS = {'receiving_basis', 'receiving_status', 'receiving_note', 'receiving_complete', 'receipt_uom_id',
    'receipt_ordered_qty', 'receipt_gross_qty', 'receipt_return_qty', 'receipt_net_qty',
    'receipt_pending_qty', 'receipt_backorder_qty', 'receipt_unreceived_qty'}
CLAIM_RECEIPT_FIELDS = {'receiving_summary', 'receiving_accessible', 'receipt_move_preview_ids',
    'receipt_move_count', 'receipt_count', 'receiving_review_warning', 'review_receipt_signature'}


class ClaimLineReceiving(models.Model):
    _inherit = 'supplier.shortage.claim.line'

    receiving_basis = fields.Json(compute='_compute_receiving')
    receiving_status = fields.Selection(RECEIVING_STATES, compute='_compute_receiving', string='Receiving Status')
    receiving_note = fields.Char(compute='_compute_receiving', string='Receiving Information')
    receiving_complete = fields.Boolean(compute='_compute_receiving')
    receipt_uom_id = fields.Many2one('uom.uom', compute='_compute_receiving', string='Current Receiving Purchase UoM')
    receipt_ordered_qty = fields.Float(compute='_compute_receiving', digits=0, string='Current Ordered Quantity', aggregator=False)
    receipt_gross_qty = fields.Float(compute='_compute_receiving', digits=0, string='Gross Completed Supplier Receipts', aggregator=False)
    receipt_return_qty = fields.Float(compute='_compute_receiving', digits=0, string='Linked Completed Supplier Returns', aggregator=False)
    receipt_net_qty = fields.Float(compute='_compute_receiving', digits=0, string='Net Supplier Receipt Position', aggregator=False)
    receipt_pending_qty = fields.Float(compute='_compute_receiving', digits=0, string='Pending Supplier Demand', aggregator=False)
    receipt_backorder_qty = fields.Float(compute='_compute_receiving', digits=0, string='Pending Supplier Backorders', aggregator=False)
    receipt_unreceived_qty = fields.Float(compute='_compute_receiving', digits=0, string='Not Yet Received (Gross Basis)', aggregator=False)

    @api.depends('purchase_line_id', 'product_id', 'company_id', 'claim_id.purchase_id', 'purchase_line_id.company_id', 'purchase_line_id.order_id', 'purchase_line_id.product_qty', 'purchase_line_id.uom_id.factor',
                 'purchase_line_id.move_ids.state', 'purchase_line_id.move_ids.quantity',
                 'purchase_line_id.move_ids.product_uom_qty', 'purchase_line_id.move_ids.returned_move_ids.state')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_receiving(self):
        self.check_access('read')
        try:
            results = self.purchase_line_id._ssc_receiving_analysis()
        except AccessError:
            results = {line.id: empty_position('unavailable', self.env._('Your permissions do not allow complete receipt reconciliation.'), False) for line in self.purchase_line_id}
        for line in self:
            data = results.get(line.purchase_line_id.id, empty_position('requires_review', self.env._('Link a supported purchase line before reconciling receipts.')))
            if data['accessible'] and line.purchase_line_id and (line.purchase_line_id.company_id != line.company_id or line.purchase_line_id.order_id != line.claim_id.purchase_id):
                data = empty_position('requires_review', self.env._('Purchase source ownership changed. Correct the company and purchase-line relationship before using receiving evidence.'))
            if data.get('complete') and line.product_id != line.purchase_line_id.product_id:
                data = empty_position('requires_review', self.env._('Claim product no longer matches the purchase line. Correct the relationship before using receiving evidence.'))
            line.receiving_basis = data
            line.receiving_status = data['status']
            line.receiving_note = data['note']
            line.receiving_complete = data['complete']
            line.receipt_uom_id = data['uom_id']
            for field, key in [('receipt_ordered_qty','ordered'),('receipt_gross_qty','gross'),('receipt_return_qty','returned'),
                ('receipt_net_qty','net'),('receipt_pending_qty','pending'),('receipt_backorder_qty','backorder'),('receipt_unreceived_qty','unreceived')]:
                line[field] = data[key]

    @api.model_create_multi
    def create(self, vals_list):
        if any(LINE_RECEIPT_FIELDS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Receiving evidence is derived from stock documents and cannot be supplied manually.'))
        lines = super().create(vals_list)
        lines.invalidate_recordset(list(LINE_RECEIPT_FIELDS))
        return lines

    def write(self, vals):
        if LINE_RECEIPT_FIELDS & vals.keys():
            raise AccessError(self.env._('Receiving evidence is read-only. Use standard Inventory operations to change stock records.'))
        return super().write(vals)


class ClaimReceiving(models.Model):
    _inherit = 'supplier.shortage.claim'

    receiving_summary = fields.Html(compute='_compute_receiving_context', string='Goods Receipt Reconciliation')
    receiving_accessible = fields.Boolean(compute='_compute_receiving_context')
    receipt_move_preview_ids = fields.Many2many('stock.move', compute='_compute_receiving_context', string='Receiving Evidence (40-row Preview)')
    receipt_move_count = fields.Integer(compute='_compute_receiving_context', string='Related Movements')
    receipt_count = fields.Integer(compute='_compute_receiving_context', string='Related Receipt Operations')
    receiving_review_warning = fields.Char(compute='_compute_receiving_context', string='Receiving Review Information')
    review_receipt_signature = fields.Char(readonly=True, copy=False)

    def _receiving_positions(self):
        self.ensure_one()
        self.check_access('read')
        if self.claim_line_ids:
            return {line.purchase_line_id.id or 0: line.receiving_basis for line in self._review_lines()}
        if not self.purchase_id:
            return {}
        try:
            self.purchase_id._check_shortage_claim_source_access()
            return self.purchase_id.order_line._ssc_receiving_analysis()
        except AccessError:
            return {0: empty_position('unavailable', self.env._('Your permissions do not allow complete receipt reconciliation.'), False)}

    def _receiving_fingerprint(self, positions):
        if not positions or not all(data.get('complete') and data.get('signature') for data in positions.values()):
            return False
        return hashlib.sha256(json.dumps({str(key): value['signature'] for key,value in positions.items()}, sort_keys=True).encode()).hexdigest()

    @api.depends('purchase_id', 'state', 'claim_line_ids.receiving_basis', 'review_receipt_signature',
                 'purchase_id.order_line.move_ids.state', 'purchase_id.order_line.move_ids.quantity')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_receiving_context(self):
        titles = dict(RECEIVING_STATES)
        for claim in self:
            positions = claim._receiving_positions()
            accessible = all(data['accessible'] for data in positions.values())
            move_ids = sorted({identifier for data in positions.values() for identifier in data['move_ids']}) if accessible else []
            picking_ids = {identifier for data in positions.values() for identifier in data['picking_ids']} if accessible else set()
            claim.receiving_accessible = accessible
            claim.receipt_move_preview_ids = self.env['stock.move'].browse(move_ids[:40])
            claim.receipt_move_count = len(move_ids)
            claim.receipt_count = len(picking_ids)
            rows = []
            for identifier, data in positions.items():
                label = self.env._('Unlinked / unavailable purchase line')
                if identifier > 0 and data['accessible']:
                    label = self.env['purchase.order.line'].browse(identifier).display_name
                if data['complete']:
                    unit = self.env['uom.uom'].browse(data['uom_id']).display_name
                    cells = [formatLang(self.env, data[key], digits=self.env['decimal.precision'].precision_get('Product Unit'))
                             for key in ('ordered','gross','returned','net','pending','backorder','unreceived')]
                else:
                    unit, cells = '', [self.env._('Unavailable / review')] * 7
                rows.append(Markup('<tr><td>%s</td>%s<td>%s</td><td>%s</td><td>%s</td></tr>') % (
                    label, Markup('').join(Markup('<td>%s</td>') % cell for cell in cells), unit, titles[data['status']], data['note']))
            heading = [self.env._(title) for title in ('PO Line','Ordered','Gross Received','Supplier Returns','Net Supplier Position',
                       'Pending Supplier Demand','Pending Backorders','Not Yet Received (Gross)','Purchase UoM','Status','Information')]
            claim.receiving_summary = Markup('<div class="table-responsive"><table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>') % (
                Markup('').join(Markup('<th>%s</th>') % label for label in heading), Markup('').join(rows)) if rows else self.env._('Select purchase lines to view receiving evidence.')
            warning = False
            if claim.state == 'under_review':
                signature = claim._receiving_fingerprint(positions)
                if not signature:
                    warning = self.env._('Receiving evidence is incomplete or requires review. It cannot establish an approved shortage or recovery.')
                elif not claim.review_receipt_signature:
                    warning = self.env._('No complete receiving baseline was recorded at submission. Review current receiving evidence before any later approval.')
                elif signature != claim.review_receipt_signature:
                    warning = self.env._('Receiving operations changed after submission. Submitted valuation and claim history are preserved; correction and re-review may be required.')
            claim.receiving_review_warning = warning

    def _receiving_action(self, picking):
        self.ensure_one()
        self.check_access('read')
        positions = self._receiving_positions()
        if not all(data['accessible'] for data in positions.values()):
            raise AccessError(self.env._('Your permissions do not allow complete receipt reconciliation.'))
        ids = sorted({identifier for data in positions.values() for identifier in data['picking_ids' if picking else 'move_ids']})
        model = 'stock.picking' if picking else 'stock.move'
        self.env[model].browse(ids).check_access('read')
        action = {'type':'ir.actions.act_window', 'name':self.env._('Related Receipt Operations' if picking else 'Receiving Movement Evidence'),
                  'res_model':model, 'view_mode':'list,form', 'domain':[('id','in',ids)], 'limit':80,
                  'context':{'create':False,'edit':False,'delete':False}}
        if not picking:
            action['views'] = [(self.env.ref('supplier_shortage_claim_reconciliation.view_receipt_move_list').id,'list'),
                               (self.env.ref('supplier_shortage_claim_reconciliation.view_receipt_move_form').id,'form')]
        return action

    def action_view_related_receipts(self):
        return self._receiving_action(True)

    def action_view_receiving_movements(self):
        return self._receiving_action(False)

    @api.model_create_multi
    def create(self, vals_list):
        if any(CLAIM_RECEIPT_FIELDS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Receiving context and review baseline are server-controlled.'))
        prepared = [dict(vals, review_receipt_signature=False) for vals in vals_list]
        claims = super().create(prepared)
        claims.invalidate_recordset(list(CLAIM_RECEIPT_FIELDS))
        return claims

    def write(self, vals):
        if CLAIM_RECEIPT_FIELDS & vals.keys():
            raise AccessError(self.env._('Receiving context and review baseline are read-only.'))
        return super().write(vals)

    def action_submit_for_review(self):
        with self.env.cr.savepoint():
            result = super().action_submit_for_review()
            for claim in self:
                signature = claim._receiving_fingerprint(claim._receiving_positions())
                # Capture only a source fingerprint, never a duplicate stock quantity ledger.
                super(ClaimReceiving, claim).write({'review_receipt_signature': signature})
            return result
