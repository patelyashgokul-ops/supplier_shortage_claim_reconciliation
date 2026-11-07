import hashlib
import json
import math
from collections import defaultdict

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError


RECEIVING_STATES = [
    ('no_receipt', 'No Completed Receipt'), ('pending', 'Pending Receipt'),
    ('partial', 'Partially Received'), ('full', 'Fully Received'), ('over', 'Over-Received'),
    ('returns', 'Supplier Return Activity'), ('requires_review', 'Requires Review'),
    ('unavailable', 'Receiving Access Incomplete'),
]
FLOW_DOMAIN = [('location_id.usage', 'in', ['supplier', 'internal', 'transit']),
               ('location_dest_id.usage', 'in', ['supplier', 'internal', 'transit'])]


def empty_position(status, note, accessible=True):
    return dict(status=status, note=note, accessible=accessible, complete=False,
                ordered=0, gross=0, returned=0, net=0, pending=0, backorder=0, unreceived=0,
                cancelled=0, issues=[], move_ids=[], picking_ids=[], move_kinds={}, signature=False, uom_id=False)


def _coverage_search(model, domain):
    return model.search(Domain(domain, internal=True))


class PurchaseLineReceiving(models.Model):
    _inherit = 'purchase.order.line'

    def _ssc_receiving_analysis(self):
        """Live PO-line positions; no source writes or elevated business reads."""
        self.check_access('read')
        results = {}
        for order in self.order_id:
            order._check_shortage_claim_source_access()
        eligible = self.filtered(lambda line: not line.display_type and not line.is_downpayment
                                 and line.product_id.type == 'consu' and line.qty_received_method == 'stock_moves'
                                 and line.uom_id and line.uom_id.compare(line.product_qty, 0) > 0)
        for line in self - eligible:
            results[line.id] = empty_position('requires_review', self.env._('This purchase line has no supported physical receiving basis.'))
        if not eligible:
            return results
        Move = self.env['stock.move']
        try:
            Move.check_access('read')
            self.env['stock.picking'].check_access('read')
            self.env['stock.move.line'].check_access('read')
            # A narrow ORM coverage probe returns only already-authorized PO-line IDs.
            # It never reads or returns hidden move identities, quantities or counts.
            blocked = _coverage_search(eligible, [('id', 'in', eligible.ids),
                ('move_ids', 'any!', ['!', ('id', 'access', 'read')])])
            if blocked:
                raise AccessError(self.env._('Your permissions do not allow complete receipt reconciliation.'))
            moves = Move.search([('purchase_line_id', 'in', eligible.ids)])
            frontier = moves
            forward_ids = set(moves.ids)
            while frontier:
                hidden_parent = _coverage_search(Move, [('id', 'in', frontier.ids), '|',
                    ('move_orig_ids', 'any!', ['!', ('id', 'access', 'read')]),
                    ('origin_returned_move_id', 'any!', ['!', ('id', 'access', 'read')])])
                forward = frontier.filtered(lambda move: move.id in forward_ids)
                hidden_child = _coverage_search(Move, [('id', 'in', forward.ids), '|',
                    ('move_dest_ids', 'any!', FLOW_DOMAIN + ['!', ('id', 'access', 'read')]),
                    ('returned_move_ids', 'any!', FLOW_DOMAIN + ['!', ('id', 'access', 'read')])]) if forward else Move
                if hidden_parent or hidden_child:
                    raise AccessError(self.env._('Your permissions do not allow complete receipt provenance verification.'))
                parents = frontier.move_orig_ids | frontier.origin_returned_move_id
                children = Move.search(FLOW_DOMAIN + ['|', ('move_orig_ids', 'in', forward.ids),
                    ('origin_returned_move_id', 'in', forward.ids)]) if forward else Move
                forward_ids.update(children.ids)
                following = (parents | children) - moves
                moves |= following
                frontier = following
            moves.check_access('read')
            moves.picking_id.check_access('read')
            (moves.location_id | moves.location_dest_id).check_access('read')
            (moves.picking_type_id.warehouse_id | moves.location_dest_id.warehouse_id | moves.location_id.warehouse_id).check_access('read')
            hidden_details = _coverage_search(Move, [('id', 'in', moves.ids),
                ('move_line_ids', 'any!', ['!', ('id', 'access', 'read')])]) if moves else Move
            if hidden_details:
                raise AccessError(self.env._('Your permissions do not allow complete movement-line traceability.'))
            moves.move_line_ids.check_access('read')
            ancestors, visited = moves.picking_id.backorder_id, self.env['stock.picking']
            while ancestors:
                ancestors.check_access('read')
                if _coverage_search(ancestors, [('id', 'in', ancestors.ids), ('move_ids', 'any!', ['!', ('id', 'access', 'read')])]):
                    raise AccessError(self.env._('Backorder provenance is not fully accessible.'))
                ancestors.move_ids.check_access('read')
                visited |= ancestors
                ancestors = ancestors.backorder_id - visited
        except AccessError:
            for line in eligible:
                results[line.id] = empty_position('unavailable', self.env._('Your permissions do not allow complete receipt reconciliation.'), False)
            return results

        origins = {}
        def purchase_origins(move, visiting=None):
            if move.id in origins:
                return origins[move.id]
            visiting = set(visiting or ())
            if move.id in visiting:
                raise ValidationError(self.env._('Movement provenance is cyclic; manual review is required.'))
            visiting.add(move.id)
            ids = set(move.purchase_line_id.ids)
            for parent in move.move_orig_ids | move.origin_returned_move_id:
                ids.update(purchase_origins(parent, visiting))
            origins[move.id] = ids
            return ids

        def has_supplier_root(move, visiting=None):
            visiting = set(visiting or ())
            if move.id in visiting:
                return False
            visiting.add(move.id)
            if (move.state == 'done' and not move.origin_returned_move_id and move.purchase_line_id
                    and move.location_id.usage == 'supplier' and move.location_dest_id.usage == 'internal'):
                return True
            return any(has_supplier_root(parent, visiting) for parent in move.move_orig_ids | move.origin_returned_move_id)

        by_line = defaultdict(list)
        graph_problem = False
        for move in moves:
            try:
                line_ids = purchase_origins(move)
            except ValidationError:
                graph_problem = True
                line_ids = set(eligible.ids)
            for line_id in line_ids & set(eligible.ids):
                by_line[line_id].append((move, line_ids))

        for line in eligible:
            data = empty_position('no_receipt', self.env._('No completed supplier receipt was found.'))
            data.update(complete=True, ordered=line.product_qty, uom_id=line.uom_id.id)
            reasons = []
            signatures = []
            relevant_ids, picking_ids = set(), set()
            has_boundary = False
            for move, line_ids in by_line[line.id]:
                # Other PO roots may be loaded only to disambiguate shared internal provenance.
                if move.purchase_line_id and move.purchase_line_id != line:
                    if move.location_id.usage == 'supplier' and not move.origin_returned_move_id:
                        continue
                try:
                    kind = move._ssc_receipt_kind(line, line_ids, has_supplier_root)
                    warehouse = move.picking_type_id.warehouse_id or move.location_dest_id.warehouse_id
                    coherent = (move.company_id == line.company_id
                                and (not move.picking_id or move.picking_id.company_id == line.company_id)
                                and all(not loc.company_id or loc.company_id == line.company_id for loc in move.location_id | move.location_dest_id)
                                and (not warehouse or warehouse.company_id == line.company_id))
                    if not coherent:
                        raise ValidationError(self.env._('Receiving company or warehouse does not match the purchase line.'))
                    relevant_ids.add(move.id)
                    data['move_kinds'][str(move.id)] = kind
                    signatures.append([move.id, move.state, move.quantity if move.state == 'done' else None,
                        move.product_uom_qty, move.uom_id.id, move.uom_id.factor,
                        move.location_id.id, move.location_id.usage, move.location_dest_id.id, move.location_dest_id.usage,
                        move.picking_id.id, move.picking_id.backorder_id.id, move.origin_returned_move_id.id,
                        move.date.isoformat() if move.date else None, warehouse.id])
                    if kind in ('receipt', 'replacement', 'return'):
                        has_boundary = True
                        original = move.picking_id.backorder_id
                        if move.state not in ('done', 'cancel') and original and (original.state not in ('done', 'cancel') or line not in original.move_ids.purchase_line_id):
                            data['issues'].append('inconsistent_backorder')
                            reasons.append(self.env._('Backorder lineage has overlapping active ancestors or inconsistent purchase-line ownership. Active demand cannot be trusted until reviewed.'))
                        picking_ids.update(move.picking_id.ids)
                        quantity = move._ssc_normalized_quantity(line, move.quantity if move.state == 'done' else move.product_uom_qty)
                        if move.state == 'done':
                            if kind == 'return':
                                data['returned'] += quantity
                            else:
                                data['gross'] += quantity
                        elif move.state == 'cancel':
                            data['cancelled'] += quantity
                        elif kind != 'return':
                            data['pending'] += quantity
                            if move.picking_id.backorder_id:
                                data['backorder'] += quantity
                    elif kind == 'review':
                        picking_ids.update(move.picking_id.ids)
                        reasons.append(self.env._('Stock movement requires purchase-line, supplier boundary or return provenance verification.'))
                except ValidationError as error:
                    if not move.uom_id._has_common_reference(line.uom_id):
                        data['issues'].append('incompatible_uom')
                    reasons.append(str(error))
            data.update(move_ids=sorted(relevant_ids), picking_ids=sorted(picking_ids))
            if graph_problem:
                reasons.append(self.env._('Movement provenance requires manual verification.'))
            if relevant_ids and not has_boundary and not reasons:
                reasons.append(self.env._('Related internal or other movements do not establish supplier receiving evidence.'))
            if reasons or line.uom_id.compare(data['returned'], data['gross']) > 0:
                data.update(status='requires_review', complete=False,
                            note=' '.join(dict.fromkeys(reasons)) or self.env._('Supplier returns exceed verified gross receipts.'))
            else:
                data['net'] = data['gross'] - data['returned']
                difference = line.product_qty - data['gross']
                data['unreceived'] = difference if line.uom_id.compare(difference, 0) > 0 else 0
                comparison = line.uom_id.compare(data['gross'], line.product_qty)
                if line.uom_id.compare(data['returned'], 0) > 0:
                    data.update(status='returns', note=self.env._('Supplier return activity affects the receiving position; it is not an original shortage determination.'))
                elif comparison > 0:
                    data.update(status='over', note=self.env._('Received quantity exceeds ordered quantity.'))
                elif comparison == 0:
                    data.update(status='full', note=self.env._('Completed supplier receipts match the ordered quantity; financial entitlement is not verified.'))
                elif line.uom_id.compare(data['gross'], 0) > 0:
                    data.update(status='partial', note=self.env._('Partial receipts are recorded. Pending supplier delivery requires review, not automatic shortage confirmation.'))
                elif line.uom_id.compare(data['pending'], 0) > 0:
                    data.update(status='pending', note=self.env._('Pending supplier delivery requires review. Unvalidated quantities are not completed receipts.'))
                if data['cancelled']:
                    data['note'] += ' ' + self.env._('Cancelled receiving operations are present; cancellation alone does not establish supplier liability.')
            signature_data = [line.id, line.product_id.id, line.product_qty, line.uom_id.id, line.uom_id.factor, sorted(signatures)]
            data['signature'] = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest() if data['complete'] else False
            results[line.id] = data
        return results


class StockMoveReceiving(models.Model):
    _inherit = 'stock.move'

    ssc_receipt_kind = fields.Selection([('receipt', 'Supplier Receipt'), ('replacement', 'Later Replacement Receipt'),
        ('return', 'Supplier Return'), ('internal', 'Internal / Transit'), ('excluded', 'Excluded Movement'),
        ('review', 'Requires Review'), ('unavailable', 'Source Access Incomplete')], compute='_compute_ssc_receiving')
    ssc_completed_purchase_qty = fields.Float(string='Completed Supplier Quantity (Purchase UoM)', digits=0, compute='_compute_ssc_receiving')
    ssc_purchase_uom_id = fields.Many2one('uom.uom', compute='_compute_ssc_receiving', string='Purchase UoM')
    ssc_warehouse_id = fields.Many2one('stock.warehouse', compute='_compute_ssc_receiving', string='Warehouse')
    ssc_purchase_line_id = fields.Many2one('purchase.order.line', compute='_compute_ssc_receiving', string='Verified PO Line')
    ssc_quantity_available = fields.Boolean(compute='_compute_ssc_receiving')
    ssc_backorder_id = fields.Many2one(related='picking_id.backorder_id', compute_sudo=False, string='Backorder Of')

    def _ssc_receipt_kind(self, line, line_ids, has_supplier_root):
        source, destination = self.location_id.usage, self.location_dest_id.usage
        if self.product_id != line.product_id:
            return 'review'
        if source in ('internal', 'transit') and destination in ('internal', 'transit'):
            return 'internal'
        if source == 'supplier' and destination == 'internal':
            if len(line_ids) != 1 or not self.picking_id:
                return 'review'
            if self.origin_returned_move_id:
                parent = self.origin_returned_move_id
                if parent.state != 'done' or parent.location_dest_id.usage != 'supplier' or not has_supplier_root(parent):
                    return 'review'
                return 'replacement'
            return 'receipt' if self.purchase_line_id == line and self.picking_type_id.code == 'incoming' else 'review'
        if source == 'internal' and destination == 'supplier':
            parent = self.origin_returned_move_id
            if len(line_ids) == 1 and self.picking_id and parent and parent.state == 'done' and has_supplier_root(parent):
                return 'return'
            return 'review'
        if source == 'supplier' or destination == 'supplier':
            return 'review'
        return 'excluded'

    def _ssc_normalized_quantity(self, line, quantity):
        if (not math.isfinite(quantity) or quantity < 0 or not self.uom_id._has_common_reference(line.uom_id)
                or not math.isfinite(self.uom_id.factor) or self.uom_id.factor <= 0
                or not math.isfinite(line.uom_id.factor) or line.uom_id.factor <= 0):
            raise ValidationError(self.env._('Incompatible receiving and purchase units of measure or invalid movement quantity.'))
        converted = self.uom_id._compute_quantity(quantity, line.uom_id, round=False)
        if not math.isfinite(converted) or (quantity > 0 and line.uom_id.is_zero(converted)):
            raise ValidationError(self.env._('Receiving conversion cannot be represented reliably.'))
        return converted

    @api.depends('state', 'quantity', 'uom_id.factor', 'purchase_line_id', 'origin_returned_move_id',
                 'location_id.usage', 'location_dest_id.usage', 'picking_id.backorder_id')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_ssc_receiving(self):
        for move in self:
            move.ssc_receipt_kind = 'unavailable'
            move.ssc_completed_purchase_qty = 0
            move.ssc_purchase_uom_id = False
            move.ssc_purchase_line_id = False
            move.ssc_quantity_available = False
            move.ssc_warehouse_id = False
        try:
            self.check_access('read')
            self.picking_id.check_access('read')
            graph, frontier = self, self
            while frontier:
                parents = (frontier.move_orig_ids | frontier.origin_returned_move_id) - graph
                parents.check_access('read')
                graph |= parents
                frontier = parents
            lines = graph.purchase_line_id
            lines.check_access('read')
            analysis = lines._ssc_receiving_analysis() if lines else {}
            warehouses = self.picking_type_id.warehouse_id | self.location_dest_id.warehouse_id | self.location_id.warehouse_id
            warehouses.check_access('read')
            for move in self:
                candidates = lines.filtered(lambda line: move.id in analysis[line.id]['move_ids'])
                line = move.purchase_line_id or (candidates if len(candidates) == 1 else self.env['purchase.order.line'])
                if not line:
                    move.ssc_receipt_kind = 'internal' if move.location_id.usage in ('internal','transit') and move.location_dest_id.usage in ('internal','transit') else 'review'
                    continue
                data = analysis.get(line.id, {})
                if not data.get('accessible'):
                    continue
                kind = data.get('move_kinds', {}).get(str(move.id), 'review')
                move.ssc_receipt_kind = kind
                move.ssc_purchase_uom_id = line.uom_id
                move.ssc_purchase_line_id = line
                move.ssc_warehouse_id = move.picking_type_id.warehouse_id or move.location_dest_id.warehouse_id
                if move.state == 'done' and kind in ('receipt','replacement','return'):
                    try:
                        move.ssc_completed_purchase_qty = move._ssc_normalized_quantity(line, move.quantity)
                        move.ssc_quantity_available = True
                    except ValidationError:
                        move.ssc_receipt_kind = 'review'
        except AccessError:
            return

    @api.model_create_multi
    def create(self, vals_list):
        if any(any(key.startswith('ssc_') for key in vals) for vals in vals_list):
            raise AccessError(self.env._('Receiving evidence fields are read-only source projections.'))
        moves = super().create(vals_list)
        moves.invalidate_recordset([name for name in self._fields if name.startswith('ssc_')])
        return moves

    def write(self, vals):
        if any(key.startswith('ssc_') for key in vals):
            raise AccessError(self.env._('Receiving evidence fields are read-only source projections.'))
        return super().write(vals)

    def action_ssc_open_picking(self):
        self.ensure_one()
        self.check_access('read')
        self.picking_id.check_access('read')
        if not self.picking_id:
            raise ValidationError(self.env._('This movement has no picking document.'))
        return {'type':'ir.actions.act_window', 'name':self.env._('Receiving Operation'), 'res_model':'stock.picking',
                'view_mode':'form', 'views':[(False,'form')], 'res_id':self.picking_id.id,
                'context':{'create':False, 'edit':False}}
