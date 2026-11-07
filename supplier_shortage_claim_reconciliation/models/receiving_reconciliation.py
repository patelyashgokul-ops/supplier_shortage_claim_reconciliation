import hashlib
import json
import math

from markupsafe import Markup
from psycopg2 import IntegrityError

from odoo import api, fields, models, Command
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tools import formatLang

from .receipt_analysis import _coverage_search


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class PurchaseLineIntegrity(models.Model):
    _inherit = 'purchase.order.line'

    def _ssc_receiving_integrity(self, position):
        self.ensure_one()
        if not position['accessible']:
            raise AccessError(self.env._('Your permissions do not allow complete receipt reconciliation.'))
        moves = self.env['stock.move'].browse(position['move_ids'])
        moves.check_access('read')
        result = dict(complete=position['complete'], difference=0, over=0,
                      canceled=position['cancelled'], native=0, native_expected=0,
                      native_conflict=False, explanation=self.env._('Receiving basis requires review.'))
        # Source fingerprint is independent of translated descriptions or review decisions.
        result['signature'] = signature([self.id, self.product_qty, self.uom_id.id, self.uom_id.factor,
            self.company_id.id, self.order_id.id, self.qty_received, self.qty_received_method,
            [[m.id, m.state, m.quantity, m.product_uom_qty, m.product_id.id, m.uom_id.id, m.uom_id.factor,
              m.purchase_line_id.id, m.company_id.id, m.location_id.id, m.location_dest_id.id,
              m.origin_returned_move_id.id, m.to_refund, m.picking_id.backorder_id.id, m.move_orig_ids.ids]
             for m in moves.sorted('id')]])
        if not position['complete']:
            return result
        difference = position['ordered'] - position['net']
        result['difference'] = difference if self.uom_id.compare(difference, 0) > 0 else 0
        result['over'] = -difference if self.uom_id.compare(difference, 0) < 0 else 0
        # Verify the native purchase policy without calling _prepare_qty_received:
        # that native helper can write tracking messages. Physical totals above remain Step 06's.
        expected = 0
        for move in self.move_ids.filtered(lambda m: m.state == 'done' and m.product_id == self.product_id):
            amount = move.uom_id._compute_quantity(move.quantity, self.uom_id, rounding_method='HALF-UP')
            if move._is_purchase_return():
                if not move.origin_returned_move_id or move.to_refund:
                    expected -= amount
            elif move.origin_returned_move_id and move.origin_returned_move_id._is_dropshipped() and not move._is_dropshipped_returned():
                continue
            elif (move.origin_returned_move_id and move.origin_returned_move_id._is_purchase_return() and not move.to_refund) or not move._should_count_for_quantity_received():
                continue
            else:
                expected += amount
        native = self.qty_received
        result.update(native=native, native_expected=expected)
        result['native_conflict'] = not math.isfinite(native) or self.uom_id.compare(native, expected) != 0
        if result['native_conflict']:
            result['explanation'] = self.env._('Native received quantity differs from its verified movement/return policy. Verify the source; neither value was overwritten.')
        elif self.uom_id.compare(native, position['net']) != 0:
            result['explanation'] = self.env._('Native received quantity follows Odoo return/refund flags and rounding. Its difference from the physical net position is explained by that policy.')
        else:
            result['explanation'] = self.env._('Native received quantity and physical net position are consistent.')
        return result


class ClaimLineIntegrity(models.Model):
    _inherit = 'supplier.shortage.claim.line'

    receiving_integrity = fields.Json(compute='_compute_integrity')
    receipt_delivery_difference_qty = fields.Float(compute='_compute_integrity', digits=0, aggregator=False, string='Unreceived Difference (Net Basis)')
    receipt_over_received_qty = fields.Float(compute='_compute_integrity', digits=0, aggregator=False, string='Over-Received (Net Basis)')
    receipt_canceled_demand_qty = fields.Float(compute='_compute_integrity', digits=0, aggregator=False, string='Canceled Supplier Demand (Historical)')
    receiving_integrity_note = fields.Char(compute='_compute_integrity')

    @api.depends('receiving_basis', 'purchase_line_id.qty_received', 'purchase_line_id.move_ids.to_refund')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_integrity(self):
        self.check_access('read')
        for line in self:
            data = line.receiving_basis
            detail = line.purchase_line_id._ssc_receiving_integrity(data) if line.purchase_line_id and data['accessible'] else {}
            line.receiving_integrity = detail
            line.receipt_delivery_difference_qty = detail.get('difference', 0)
            line.receipt_over_received_qty = detail.get('over', 0)
            line.receipt_canceled_demand_qty = detail.get('canceled', 0)
            line.receiving_integrity_note = detail.get('explanation', data['note'])

    @api.model_create_multi
    def create(self, vals_list):
        names = {'receiving_integrity', 'receipt_delivery_difference_qty', 'receipt_over_received_qty', 'receipt_canceled_demand_qty', 'receiving_integrity_note'}
        if any(names & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Receiving integrity fields are source-derived.'))
        lines = super().create(vals_list)
        lines.invalidate_recordset(list(names))
        return lines

    def write(self, vals):
        if {'receiving_integrity', 'receipt_delivery_difference_qty', 'receipt_over_received_qty', 'receipt_canceled_demand_qty', 'receiving_integrity_note'} & vals.keys():
            raise AccessError(self.env._('Receiving integrity fields are source-derived.'))
        return super().write(vals)


CLAIM_SCAN_FIELDS = {'receiving_reconciled_at', 'receiving_scan_basis', 'receiving_exception_ids', 'receiving_exception_count', 'receiving_integrity_summary'}


class ClaimReceivingReconciliation(models.Model):
    _inherit = 'supplier.shortage.claim'

    receiving_reconciled_at = fields.Datetime(readonly=True, copy=False, string='Last Receiving Refresh')
    receiving_scan_basis = fields.Json(readonly=True, copy=False)
    receiving_integrity_summary = fields.Html(compute='_compute_integrity_summary', string='Current Receiving Integrity')
    receiving_exception_ids = fields.Many2many('supplier.shortage.receipt.exception', compute='_compute_receiving_exceptions', string='Receiving Exceptions')
    receiving_exception_count = fields.Integer(compute='_compute_receiving_exceptions', string='Open Receiving Exceptions')

    @api.depends('claim_line_ids.receiving_basis', 'claim_line_ids.receiving_integrity')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_integrity_summary(self):
        self.check_access('read')
        for claim in self:
            rows = []
            for line in claim.claim_line_ids[:40]:
                data, detail = line.receiving_basis, line.receiving_integrity
                label = line.purchase_line_id.display_name if data['accessible'] and line.purchase_line_id else self.env._('Source unavailable')
                if data['complete']:
                    values = [data[key] for key in ('ordered', 'gross', 'returned', 'net', 'pending', 'backorder')]
                    values += [detail.get(key, 0) for key in ('canceled', 'difference', 'over')]
                    cells = [formatLang(self.env, value, digits=self.env['decimal.precision'].precision_get('Product Unit')) for value in values]
                    unit = line.receipt_uom_id.display_name
                else:
                    cells, unit = [self.env._('Unknown / review')] * 9, ''
                rows.append(Markup('<tr><td>%s</td>%s<td>%s</td><td>%s</td></tr>') % (label,
                    Markup('').join(Markup('<td>%s</td>') % cell for cell in cells), unit, detail.get('explanation', data['note'])))
            headings = ('PO Line', 'Ordered', 'Gross Received', 'Supplier Returned', 'Net Received', 'Pending Delivery',
                        'Pending Backorders', 'Historical Canceled Demand', 'Unreceived (Net)', 'Over-Received (Net)', 'Purchase UoM', 'Integrity Information')
            claim.receiving_integrity_summary = Markup('<div class="table-responsive"><table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table><p>%s</p></div>') % (
                Markup('').join(Markup('<th>%s</th>') % self.env._(title) for title in headings), Markup('').join(rows),
                self.env._('40-line preview. Open individual claim lines for full receiving detail.')) if rows else self.env._('Select purchase lines to review receiving integrity.')

    def _exception_domain(self):
        return [('reconciliation_kind', '=', 'receiving'), ('purchase_id', 'in', self.purchase_id.ids), '|',
                ('purchase_line_id', 'in', self.claim_line_ids.purchase_line_id.ids), '|',
                ('claim_id', 'in', self.ids), '&', ('purchase_line_id', '=', False), ('claim_id', '=', False)]

    @api.depends('purchase_id', 'claim_line_ids.purchase_line_id')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_receiving_exceptions(self):
        self.check_access('read')
        exceptions = self.env['supplier.shortage.receipt.exception'].search(self._exception_domain())
        for claim in self:
            related = exceptions.filtered(lambda rec: rec.purchase_id == claim.purchase_id and
                (rec.purchase_line_id in claim.claim_line_ids.purchase_line_id or rec.claim_id == claim or not rec.purchase_line_id and not rec.claim_id))
            claim.receiving_exception_ids = related
            claim.receiving_exception_count = len(related.filtered(lambda rec: rec.condition_present and rec.review_state in ('new', 'in_review')))

    def action_view_receiving_exceptions(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': self.env._('Receiving Exceptions'),
                'res_model': 'supplier.shortage.receipt.exception', 'view_mode': 'list,form',
                'domain': self._exception_domain(), 'context': {'create': False, 'search_default_present': 1}}

    def action_reconcile_receiving(self):
        if not self.env.user.has_group('supplier_shortage_claim_reconciliation.group_claim_user'):
            raise AccessError(self.env._('Only an authorized purchase claim user may refresh receiving exceptions.'))
        self.check_access('write')
        Exception = self.env['supplier.shortage.receipt.exception']
        with self.env.cr.savepoint():
            self._lock_for_edit()
            if any(not claim.purchase_id or not claim.claim_line_ids for claim in self):
                raise ValidationError(self.env._('Select a purchase order and at least one valid claim line before refreshing.'))
            orders = self.purchase_id.sorted('id')
            for order in orders:
                order._check_shortage_claim_source_access()
            # Native row locks serialize only this explicit scan; no source field is written.
            orders.lock_for_update()
            lines = self.claim_line_ids.purchase_line_id
            for claim in self:
                for line in claim.claim_line_ids:
                    if not line.purchase_line_id or line.purchase_line_id.order_id != claim.purchase_id or line.purchase_line_id.company_id != claim.company_id:
                        raise ValidationError(self.env._('Correct the claim purchase-line ownership before reconciliation.'))
            lines.invalidate_recordset(['move_ids', 'qty_received'])
            positions = lines._ssc_receiving_analysis()
            if any(not data['accessible'] for data in positions.values()):
                raise AccessError(self.env._('Your permissions do not allow complete receipt reconciliation.'))
            findings, details = {}, {}

            def finding(order, line, category, event, data, quantity=None, moves=None, description=None, claim=None, severity='warning'):
                key = '%s/%s/%s/%s/%s' % (order.company_id.id, order.id, line.id if line else 0, category, event)
                support = moves if moves is not None else self.env['stock.move'].browse(data.get('move_ids', []))
                support.check_access('read')
                if any(move.company_id != order.company_id for move in support):
                    raise ValidationError(self.env._('Receiving evidence belongs to another company.'))
                findings[key] = {'company_id': order.company_id.id, 'purchase_id': order.id,
                    'purchase_line_id': line.id if line else False, 'claim_id': claim.id if claim else False,
                    'exception_type': category, 'severity': severity, 'quantity_known': quantity is not None,
                    'affected_quantity': quantity if quantity is not None else 0,
                    'product_uom_id': line.uom_id.id if line else False,
                    'source_move_ids': [Command.set(support.ids)],
                    'description': description or data.get('note') or self.env._('Review the receiving source documents; supplier liability is not established.'),
                    'source_signature': signature([details.get(line.id, {}).get('signature') if line else data, support.ids, category, event, quantity])}

            for line in lines:
                data = positions[line.id]
                detail = details[line.id] = line._ssc_receiving_integrity(data)
                moves = self.env['stock.move'].browse(data['move_ids'])
                if not data['complete']:
                    incompatible = moves.filtered(lambda m: not m.uom_id._has_common_reference(line.uom_id))
                    unexpected = moves.filtered(lambda m: m.location_dest_id.usage == 'supplier' and not m.origin_returned_move_id)
                    category = 'inconsistent_backorder' if 'inconsistent_backorder' in data.get('issues', []) else ('incompatible_uom' if incompatible else ('unexpected_return' if unexpected else 'ambiguous_stock_movement'))
                    finding(line.order_id, line, category, 'position', data, description=data['note'], severity='high')
                    continue
                if line.uom_id.compare(data['pending'], 0) > 0:
                    finding(line.order_id, line, 'pending_delivery', 'balance', data, data['pending'], severity='info',
                            description=self.env._('Active supplier delivery demand remains open. This is not a confirmed shortage.'))
                if line.uom_id.compare(detail['over'], 0) > 0:
                    finding(line.order_id, line, 'over_receipt', 'balance', data, detail['over'],
                            description=self.env._('Net supplier receipts exceed the ordered quantity. Review disposition; no stock or purchase quantity was changed.'))
                if detail['native_conflict']:
                    finding(line.order_id, line, 'source_quantity_mismatch', 'native', data, description=detail['explanation'], severity='high')
                for move in moves:
                    if data['move_kinds'].get(str(move.id)) not in ('receipt', 'replacement'):
                        continue
                    if move.state == 'cancel':
                        quantity = move._ssc_normalized_quantity(line, move.product_uom_qty)
                        finding(line.order_id, line, 'canceled_backorder_review', str(move.id), data, quantity, moves=move,
                            description=self.env._('Receiving operation %s was canceled. Its historical demand may overlap later deliveries or revised PO demand. Verify cancellation and replacement arrangements; supplier fault is unknown.', move.picking_id.display_name))
                    if move.state not in ('done', 'cancel') and move.picking_id.backorder_id:
                        original = move.picking_id.backorder_id
                        original.check_access('read')
                        original.move_ids.check_access('read')
                        if original.state not in ('done', 'cancel') or line not in original.move_ids.purchase_line_id:
                            finding(line.order_id, line, 'inconsistent_backorder', str(move.id), data,
                                description=self.env._('Backorder lineage is inconsistent or has overlapping active ancestors. Verify the original operation and purchase-line ownership.'), severity='high')
            # Siblings in actual native PO pickings are review evidence, never guessed allocations.
            pickings = orders.picking_ids
            pickings.check_access('read')
            hidden = _coverage_search(pickings, [('id', 'in', pickings.ids), ('move_ids', 'any!', ['!', ('id', 'access', 'read')])]) if pickings else pickings
            if hidden:
                raise AccessError(self.env._('Your permissions do not allow complete receiving operation coverage.'))
            orphan_moves = pickings.move_ids.filtered(lambda move: not move.purchase_line_id and
                move.location_id.usage == 'supplier' and move.location_dest_id.usage == 'internal')
            for move in orphan_moves:
                order_candidates = orders.filtered(lambda order: move.picking_id in order.picking_ids)
                if len(order_candidates) == 1:
                    finding(order_candidates, lines.browse(), 'missing_purchase_line', str(move.id),
                        {'move': move.id, 'state': move.state, 'quantity': move.quantity, 'demand': move.product_uom_qty}, moves=move,
                        description=self.env._('A supplier movement in a related PO receipt has no reliable purchase-line link. Verify ownership; no product-name allocation was made.'), severity='high')
            for claim in self:
                previous = claim.receiving_scan_basis or {}
                basis = {str(line.purchase_line_id.id): details[line.purchase_line_id.id]['signature'] for line in claim.claim_line_ids}
                for line in claim.claim_line_ids.purchase_line_id:
                    if str(line.id) in previous and previous[str(line.id)] != basis[str(line.id)]:
                        finding(line.order_id, line, 'receiving_source_changed', 'position', positions[line.id],
                                description=self.env._('Receiving source changed after an earlier explicit refresh. Revalidate warehouse evidence; claim quantities and submitted valuations were preserved.'))
                if claim.state == 'under_review' and claim.review_receipt_signature and claim._receiving_fingerprint(positions_for_claim(claim, positions)) != claim.review_receipt_signature:
                    finding(claim.purchase_id, lines.browse(), 'receiving_source_changed', 'claim%s' % claim.id,
                        {'signature': claim._receiving_fingerprint(positions_for_claim(claim, positions))},
                        moves=self.env['stock.move'].browse(sorted({i for line in claim.claim_line_ids for i in positions[line.purchase_line_id.id]['move_ids']})), claim=claim,
                        description=self.env._('Current receiving sources differ from this claim submission baseline. Internal revalidation is required; the claim was not automatically reopened.'))
                # Keep an existing source-change condition until its source changes again or a human decides.
                stable = Exception.search([('purchase_line_id', 'in', claim.claim_line_ids.purchase_line_id.ids), ('exception_type', '=', 'receiving_source_changed'), ('condition_present', '=', True)])
                for rec in stable:
                    if rec.source_key not in findings and previous.get(str(rec.purchase_line_id.id)) == basis.get(str(rec.purchase_line_id.id)):
                        findings[rec.source_key] = {name: rec[name] for name in ('company_id', 'purchase_id', 'purchase_line_id', 'claim_id', 'product_uom_id')}
                        findings[rec.source_key] = {key: value.id for key,value in findings[rec.source_key].items()}
                        findings[rec.source_key].update(exception_type=rec.exception_type, severity=rec.severity, quantity_known=rec.quantity_known,
                            affected_quantity=rec.affected_quantity, description=rec.description, source_signature=rec.source_signature,
                            source_move_ids=[Command.set(rec.source_move_ids.ids)])
                super(ClaimReceivingReconciliation, claim).write({'receiving_scan_basis': basis, 'receiving_reconciled_at': fields.Datetime.now()})
            try:
                Exception._synchronize(self._exception_domain(), findings)
            except IntegrityError as error:
                if error.pgcode != '23505' or not error.diag.constraint_name.endswith('_source_unique'):
                    raise
                raise UserError(self.env._('An existing receiving review is inaccessible or changed concurrently. Verify source permissions and retry in a new transaction.')) from error
            self.invalidate_recordset(['receiving_exception_ids', 'receiving_exception_count'])
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if any(CLAIM_SCAN_FIELDS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Receiving reconciliation metadata is server-controlled.'))
        claims = super().create([dict(vals, receiving_reconciled_at=False, receiving_scan_basis=False) for vals in vals_list])
        claims.invalidate_recordset(list(CLAIM_SCAN_FIELDS))
        return claims

    def write(self, vals):
        if CLAIM_SCAN_FIELDS & vals.keys():
            raise AccessError(self.env._('Receiving reconciliation metadata is server-controlled.'))
        return super().write(vals)


def positions_for_claim(claim, positions):
    return {line.purchase_line_id.id: positions[line.purchase_line_id.id] for line in claim.claim_line_ids}
