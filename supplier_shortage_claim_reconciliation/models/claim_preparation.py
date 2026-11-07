"""Draft preparation and reservation invariants. No stock/accounting source writes."""
from odoo import api, fields, models, Command
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError

from .receipt_analysis import _coverage_search
from .receiving_exception import ReceivingException


ROLE = 'supplier_shortage_claim_reconciliation.'
LINE_OUTPUTS = {'allocated_qty', 'allocation_state', 'preparation_snapshot', 'allocation_basis', 'candidate_qty', 'available_qty'}


def lock_records(records):
    for record in records.sorted('id'):
        record.lock_for_update()


def require_claim_user(env, manager=False):
    if not env.user.has_group(ROLE + ('group_claim_manager' if manager else 'group_claim_user')):
        raise AccessError(env._('An authorized purchase claim reviewer is required.'))


class DetectionPreparation(models.Model):
    _inherit = 'supplier.shortage.receipt.exception'

    allocation_line_ids = fields.One2many('supplier.shortage.claim.line', 'detection_id', readonly=True)
    allocated_qty = fields.Float(compute='_compute_availability', digits=0, aggregator=False)
    available_qty = fields.Float(compute='_compute_availability', digits=0, aggregator=False)
    allocation_integrity_blocked = fields.Boolean(compute='_compute_availability')
    allocation_quantity_known = fields.Boolean(compute='_compute_availability',string='Reservation UoM Basis Compatible')
    preparation_eligible = fields.Boolean(compute='_compute_availability')
    allocation_revision = fields.Integer(readonly=True, default=0, copy=False)

    def _allocation_lines(self):
        self.check_access('read')
        if _coverage_search(self, [('id','in',self.ids), ('allocation_line_ids','any!',
                ['|', '!', ('id','access','read'), '!', ('claim_id','access','read')])]):
            raise AccessError(self.env._('Allocation evidence is not fully accessible; availability is unknown.'))
        return self.env['supplier.shortage.claim.line'].search([('detection_id','in',self.ids),('allocation_state','=','active')])

    @api.depends('candidate_qty', 'review_state', 'classification', 'evidence_complete',
                 'allocation_line_ids.allocated_qty', 'allocation_line_ids.allocation_state', 'allocation_line_ids.allocation_basis',
                 'product_uom_id','product_uom_id.factor')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_availability(self):
        totals = dict.fromkeys(self.ids, 0.0)
        incompatible = set()
        for line in self._allocation_lines():
            basis = line.allocation_basis or {}
            rec = line.detection_id
            if basis.get('purchase_uom_id')!=rec.product_uom_id.id or basis.get('purchase_uom_factor')!=rec.product_uom_id.factor:
                incompatible.add(rec.id)
            else:
                totals[rec.id] += line.allocated_qty
        for rec in self:
            rec.allocation_quantity_known = rec.id not in incompatible
            rec.allocated_qty = totals[rec.id] if rec.allocation_quantity_known else 0
            rec.available_qty = max(rec.candidate_qty-totals[rec.id], 0) if rec.allocation_quantity_known else 0
            rec.allocation_integrity_blocked = rec.id in incompatible or bool(rec.product_uom_id and rec.product_uom_id.compare(totals[rec.id],rec.candidate_qty)>0)
            rec.preparation_eligible = (rec.reconciliation_kind == 'detection' and rec.review_state == 'resolved'
                and rec.classification == 'shortage_candidate_review' and rec.evidence_complete
                and rec.product_uom_id.compare(rec.available_qty,0)>0 and not rec.allocation_integrity_blocked)

    def _lock_preparation(self):
        self.check_access('read')
        lock_records(self.purchase_id)
        self.purchase_line_id.check_access('read')
        lock_records(self.purchase_line_id)
        lock_records(self)
        self.invalidate_recordset()

    def _validate_current_candidate(self):
        """Batch source revalidation, independent of internal allocations/claim overlap."""
        self.check_access('read')
        if _coverage_search(self.purchase_id, [('id','in',self.purchase_id.ids), ('shortage_claim_ids','any!',
                ['|','!',('id','access','read'),('claim_line_ids','any!',['!',('id','access','read')])])]):
            raise AccessError(self.env._('Prior claim evidence is inaccessible; claim preparation is blocked.'))
        unresolved = self.env['supplier.shortage.claim.line'].search([('purchase_line_id','in',self.purchase_line_id.ids),
            ('detection_id','=',False),('allocation_state','!=','released'),('claim_id.state','=','under_review')])
        if unresolved:
            raise ValidationError(self.env._('Existing review claims have unallocated legacy quantities. Obtain authorized draft correction and allocation reconciliation before preparing overlapping source quantities: %s',', '.join(unresolved.claim_id.mapped('name'))))
        lines = self.purchase_line_id
        if _coverage_search(lines,[('id','in',lines.ids),('shortage_review_exception_ids','any!',['!',('id','access','read')])]):
            raise AccessError(self.env._('Source review findings are inaccessible; eligibility is incomplete.'))
        blocking = self.search([('purchase_line_id','in',lines.ids),('reconciliation_kind','!=','detection'),
            ('condition_present','=',True),('severity','=','high'),('review_state','in',['new','in_review'])])
        if blocking:
            raise ValidationError(self.env._('Resolve current critical source findings before claim preparation: %s',', '.join(blocking.mapped('name'))))
        receiving, billing = lines._ssc_receiving_analysis(), lines._ssc_billing_analysis()
        stock = self.env['stock.move'].browse(sorted({mid for r in receiving.values() for mid in r['move_ids']}))
        bills = self.env['account.move.line'].browse(sorted({bid for b in billing.values() for bid in b['line_ids']}))
        stock.check_access('read')
        bills.check_access('read')
        lock_records(stock)
        lock_records(bills.move_id)
        lock_records(bills)
        for rec in self:
            line = rec.purchase_line_id
            if (rec.reconciliation_kind != 'detection' or not line or rec.purchase_id != line.order_id
                    or rec.company_id != line.company_id or rec.partner_id != line.order_id.partner_id
                    or rec.currency_id != line.currency_id or rec.product_uom_id != line.uom_id
                    or rec.review_state != 'resolved' or rec.classification != 'shortage_candidate_review'
                    or not rec.condition_present or not rec.evidence_complete or not rec.source_signature
                    or line.uom_id.compare(rec.candidate_qty,0)<=0):
                raise ValidationError(self.env._('Candidate %s is not a verified, complete shortage candidate.',rec.display_name))
            line.order_id._check_shortage_claim_source_access()
            line._check_shortage_claim_eligible()
            r,b = receiving[line.id],billing[line.id]
            if not r['accessible'] or not b['accessible']:
                raise AccessError(self.env._('Current candidate source evidence is inaccessible.'))
            integrity = line._ssc_receiving_integrity(r)
            if (not r['complete'] or not b['complete'] or integrity['native_conflict']
                    or line._ssc_detection_source_signature(r,b,integrity) != rec.source_signature):
                raise ValidationError(self.env._('Candidate %s is stale. Refresh detection and obtain renewed internal verification; historical claim snapshots are preserved.',rec.display_name))
        return True

    def _touch_allocations(self):
        # Version the candidate row: Repeatable Read transactions with stale snapshots
        # must retry rather than silently seeing a stale allocation sum after locking.
        for rec in self.sorted('id'):
            active = rec._allocation_lines()
            values = {'allocation_revision':rec.allocation_revision+1}
            if active:
                values.update(related_claim_ids=[Command.set((rec.related_claim_ids|active.claim_id).ids)],
                    claim_overlap=True,valuation_status='review',indicative_untaxed_exposure=0)
            super(ReceivingException, rec).write(values)
        self.invalidate_recordset(['allocation_line_ids','allocated_qty','available_qty','preparation_eligible'])

    @api.model
    def _synchronize(self, scope, findings):
        records = super()._synchronize(scope,findings)
        # Allocation/overlap presentation may change without physical evidence changes.
        # Refresh it without reopening a valid physical-evidence review.
        for rec in records.filtered(lambda row:row.reconciliation_kind=='detection'):
            values = findings.get(rec.source_key,{})
            changes = {}
            for name in ('claim_overlap','findings','findings_summary','valuation_status',
                         'indicative_untaxed_exposure','priority','priority_reason','description'):
                if name in values and rec[name]!=values[name]:
                    changes[name]=values[name]
            if 'related_claim_ids' in values and set(rec.related_claim_ids.ids)!=set(values['related_claim_ids'][0][2]):
                changes['related_claim_ids']=values['related_claim_ids']
            if changes:
                super(ReceivingException,rec).write(changes)
        return records

    def action_prepare_shortage_claim(self, quantities=None):
        require_claim_user(self.env)
        if not self:
            raise ValidationError(self.env._('Select at least one reviewed candidate.'))
        with self.env.cr.savepoint():
            self._lock_preparation()
            # The common ORM line-creation guard validates the entire candidate batch
            # once, under these locks. Any failure rolls back the header and its audit.
            if len(self.purchase_id) != 1 or len(self.company_id) != 1 or len(self.partner_id) != 1 or len(self.currency_id) != 1:
                raise ValidationError(self.env._('Combine only candidates from one company, supplier, Purchase Order and currency.'))
            if quantities is not None and (not isinstance(quantities,dict) or set(map(str,quantities)) != set(map(str,self.ids))):
                raise ValidationError(self.env._('Provide one purchase-UoM quantity for every selected candidate.'))
            values = []
            for rec in self:
                quantity = quantities.get(str(rec.id),quantities.get(rec.id)) if quantities is not None else rec.available_qty
                values.append(Command.create({'detection_id':rec.id,'purchase_line_id':rec.purchase_line_id.id,
                    'proposed_qty':quantity,'claim_uom_id':rec.product_uom_id.id}))
            claim = self.env['supplier.shortage.claim'].create({'purchase_id':self.purchase_id.id,
                'company_id':self.company_id.id,'partner_id':self.partner_id.id,'claim_line_ids':values})
            claim._audit('prepared',self.env._('Draft prepared from reviewed candidates: %s. No supplier acceptance or recovery.',', '.join(self.mapped('name'))),
                basis={claim.id:{str(line.id):line.preparation_snapshot for line in claim.claim_line_ids}})
        return claim._preparation_action()

    def action_prepare_claim_dialog(self):
        require_claim_user(self.env)
        self._validate_current_candidate()
        wizard = self.env['supplier.shortage.prepare.wizard'].create({'line_ids':[Command.create({
            'detection_id':rec.id,'quantity':rec.available_qty}) for rec in self]})
        return {'type':'ir.actions.act_window','res_model':wizard._name,'res_id':wizard.id,
                'view_mode':'form','target':'new'}

    def action_view_allocated_claims(self):
        lines = self._allocation_lines()
        lines.claim_id.check_access('read')
        return {'type':'ir.actions.act_window','name':self.env._('Allocated Draft / Review Claims'),
                'res_model':'supplier.shortage.claim','view_mode':'list,form','domain':[('id','in',lines.claim_id.ids)]}


class ClaimPreparation(models.Model):
    _inherit = 'supplier.shortage.claim'

    shortage_reason = fields.Text(string='Shortage Reason')
    manual_exception_reason = fields.Text(string='Manual Preparation Exception Reason')
    correction_reason = fields.Text(string='Correction / Revalidation Reason')
    evidence_ids = fields.One2many('supplier.shortage.claim.evidence','claim_id',copy=False)
    required_evidence_count = fields.Integer(related='company_id.ssc_min_verified_evidence',compute_sudo=False)
    preparation_status = fields.Selection([('incomplete','Requires Preparation'),('ready','Prepared for Validation')],compute='_compute_preparation_status')

    @api.depends('claim_line_ids.detection_id','claim_line_ids.allocation_state','claim_line_ids.allocation_basis',
        'claim_line_ids.detection_id.review_state','claim_line_ids.detection_id.source_signature','claim_line_ids.detection_id.candidate_qty',
        'shortage_reason','evidence_ids.verification_state','evidence_ids.claim_line_id','company_id.ssc_min_verified_evidence')
    def _compute_preparation_status(self):
        for claim in self:
            lines = claim._review_lines()
            sources_ready = bool(lines) and all(line.detection_id and line.allocation_state=='active'
                and line.detection_id.review_state=='resolved' and not line.detection_id.allocation_integrity_blocked
                and (line.allocation_basis or {}).get('source_signature')==line.detection_id.source_signature for line in lines)
            verified = claim.evidence_ids.filtered(lambda e:e.verification_state=='verified')
            enough_evidence = all(len(verified.filtered(lambda e:not e.claim_line_id or e.claim_line_id==line))
                >= claim.company_id.ssc_min_verified_evidence for line in lines)
            claim.preparation_status = 'ready' if sources_ready and enough_evidence and len((claim.shortage_reason or '').strip())>=5 else 'incomplete'

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access('create')
        if any({'preparation_status','required_evidence_count'} & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Preparation status is server controlled.'))
        orders = self.env['purchase.order'].browse([v.get('purchase_id',self.env.context.get('default_purchase_id'))
            for v in vals_list if v.get('purchase_id',self.env.context.get('default_purchase_id'))])
        if orders:
            orders.check_access('read')
            lock_records(orders)
        return super().create(vals_list)

    def _lock_for_edit(self):
        self.check_access('write')
        lock_records(self.purchase_id)
        return super()._lock_for_edit()

    def _preparation_action(self):
        self.ensure_one()
        self.check_access('read')
        return {'type':'ir.actions.act_window','res_model':self._name,'res_id':self.id,'view_mode':'form',
                'views':[(self.env.ref(ROLE+'view_claim_form').id,'form')],'target':'current'}

    def write(self, vals):
        if {'preparation_status','required_evidence_count'} & vals.keys():
            raise AccessError(self.env._('Preparation status is server controlled.'))
        if vals.keys() & {'shortage_reason','manual_exception_reason','correction_reason','evidence_ids'}:
            self._lock_for_edit()
            if any(claim.state!='draft' for claim in self):
                raise ValidationError(self.env._('Return to draft before changing claim preparation.'))
        return super().write(vals)

    def copy(self, default=None):
        values = dict(default or {})
        values.setdefault('manual_exception_reason','Copied as incomplete draft; obtain reviewed source allocation before submission.')
        return super().copy(values)

    def _validate_for_review(self):
        super()._validate_for_review()
        lines = self._review_lines()
        if any(not line.detection_id or line.allocation_state!='active' for line in lines):
            raise ValidationError(self.env._('Manual drafts require a reviewed detection allocation before submission. Record the manual exception reason and obtain source review; no unlimited manual quantity is allowed.'))
        candidates = lines.detection_id
        candidates._lock_preparation()
        candidates._validate_current_candidate()
        lines._validate_allocation_basis()
        lines._check_allocation_totals()
        for rec in candidates:
            if rec.product_uom_id.compare(rec.allocated_qty,rec.candidate_qty)>0:
                raise ValidationError(self.env._('Blocking allocation integrity exception: reserved quantity exceeds the current candidate. Use audited draft corrections or release; reservations were not silently released.'))
        for claim in self:
            if len((claim.shortage_reason or '').strip())<5:
                raise ValidationError(self.env._('Enter a concise shortage reason before internal review.'))
            evidence = claim.evidence_ids
            evidence.check_access('read')
            lock_records(evidence)
            evidence.invalidate_recordset()
            for line in claim._review_lines():
                suitable = evidence.filtered(lambda e:e.verification_state=='verified' and (not e.claim_line_id or e.claim_line_id==line))
                if len(suitable)<claim.company_id.ssc_min_verified_evidence:
                    raise ValidationError(self.env._('Each active line needs the configured minimum of relevant, internally verified evidence. A real source reference is acceptable; do not fabricate a GRN.'))
                suitable._check_sources()

    def action_check_preparation(self):
        require_claim_user(self.env)
        self.check_access('write')
        self._review_lines().detection_id.purchase_line_id.action_run_shortage_detection()
        return {'type':'ir.actions.client','tag':'display_notification','params':{'title':self.env._('Source Check Complete'),
            'message':self.env._('Review current detection, allocation conflicts and evidence. Historical preparation snapshots were preserved.'),'sticky':True}}

    def action_revalidate_preparation(self):
        require_claim_user(self.env,manager=True)
        with self.env.cr.savepoint():
            self._lock_for_edit()
            if any(claim.state!='draft' or len((claim.correction_reason or '').strip())<5 for claim in self):
                raise ValidationError(self.env._('Use draft claims and enter a meaningful correction reason.'))
            lines = self._review_lines()
            lines.detection_id._lock_preparation()
            lines.detection_id._validate_current_candidate()
            for rec in lines.detection_id:
                if rec.allocation_integrity_blocked:
                    raise ValidationError(self.env._('Correct or release excess/incompatible reservations before revalidation.'))
            for line in lines:
                if not line.detection_id or line.purchase_line_id.uom_id.compare(line._normalized_quantity(),line.allocated_qty)!=0:
                    raise ValidationError(self.env._('Current UoM conversion differs from the reservation; release and prepare a correct allocation.'))
                line._store_allocation_basis(initial=False)
            self._audit('allocation_revalidated',self.env._('Current reviewed source basis accepted; original preparation snapshots retained.'),
                reason='; '.join(self.mapped('correction_reason')),basis={c.id:{str(l.id):l.allocation_basis for l in c._review_lines()} for c in self})
        return True


class ClaimLineAllocation(models.Model):
    _inherit = 'supplier.shortage.claim.line'

    detection_id = fields.Many2one('supplier.shortage.receipt.exception',check_company=True,ondelete='restrict',index=True,copy=False)
    allocated_qty = fields.Float(readonly=True,digits=0,copy=False,aggregator=False)
    allocation_state = fields.Selection([('active','Reserved'),('released','Released')],readonly=True,copy=False)
    preparation_snapshot = fields.Json(readonly=True,copy=False)
    allocation_basis = fields.Json(readonly=True,copy=False)
    candidate_qty = fields.Float(related='detection_id.candidate_qty',compute_sudo=False)
    available_qty = fields.Float(related='detection_id.available_qty',compute_sudo=False)

    _active_quantity_positive = models.Constraint("CHECK (allocation_state IS DISTINCT FROM 'active' OR (detection_id IS NOT NULL AND allocated_qty > 0))",
        'Active reservations need a candidate and a positive normalized quantity.')

    def _access_domain(self, operation):
        return super()._access_domain(operation) & Domain('claim_id','access','read') & (
            Domain('detection_id','=',False) | Domain('detection_id','access','read'))

    def _store_allocation_basis(self, initial):
        from .shortage_claim_line import ShortageClaimLine
        for line in self:
            rec = line.detection_id
            basis = {'candidate_id':rec.id,'candidate_reference':rec.name,'source_signature':rec.source_signature,
                'purchase_id':rec.purchase_id.id,'purchase_line_id':rec.purchase_line_id.id,'company_id':rec.company_id.id,
                'partner_id':rec.partner_id.id,'product_id':rec.product_id.id,'purchase_uom_id':rec.product_uom_id.id,
                'purchase_uom_factor':rec.product_uom_id.factor,'claim_uom_id':line.claim_uom_id.id,
                'claim_uom_factor':line.claim_uom_id.factor,'currency_id':rec.currency_id.id,
                'candidate_qty':rec.candidate_qty,'allocated_qty':line.allocated_qty,'ordered_qty':rec.ordered_qty,
                'gross_received_qty':rec.gross_received_qty,'returned_qty':rec.returned_qty,'net_received_qty':rec.net_received_qty,
                'posted_billed_qty':rec.posted_billed_qty,'pending_qty':rec.pending_qty,'pending_backorder_qty':rec.pending_backorder_qty,
                'source_move_ids':rec.source_move_ids.ids,'source_bill_line_ids':rec.source_bill_line_ids.ids,
                'reviewed_by':rec.resolved_by.id,'reviewed_at':fields.Datetime.to_string(rec.resolved_at),
                'captured_at':fields.Datetime.to_string(fields.Datetime.now()),'provisional_valuation':line._get_live_valuation()}
            values = {'allocation_basis':basis}
            if initial:
                values['preparation_snapshot']=basis
            super(ShortageClaimLine,line).write(values)

    def _validate_allocation_basis(self):
        for line in self:
            basis = line.allocation_basis or {}
            rec = line.detection_id
            if (basis.get('source_signature')!=rec.source_signature or basis.get('purchase_uom_id')!=rec.product_uom_id.id
                    or basis.get('purchase_uom_factor')!=rec.product_uom_id.factor
                    or basis.get('claim_uom_factor')!=line.claim_uom_id.factor
                    or rec.product_uom_id.compare(line._normalized_quantity(),line.allocated_qty)!=0):
                raise ValidationError(self.env._('Allocation source/UoM changed. Obtain renewed candidate review and authorized draft revalidation.'))

    def _check_allocation_totals(self):
        for rec in self.detection_id:
            active = rec._allocation_lines()
            if any((line.allocation_basis or {}).get('purchase_uom_id')!=rec.product_uom_id.id or
                   (line.allocation_basis or {}).get('purchase_uom_factor')!=rec.product_uom_id.factor for line in active):
                raise ValidationError(self.env._('Existing reservation UoM basis changed. Release and prepare corrected allocations; incompatible units cannot be added.'))
            total = sum(active.mapped('allocated_qty'))
            if rec.product_uom_id.compare(total,rec.candidate_qty)>0:
                raise ValidationError(self.env._('Requested quantity exceeds available candidate quantity for %s.',rec.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access('create')
        if any(LINE_OUTPUTS & values.keys() for values in vals_list) or any('default_'+field in self.env.context for field in LINE_OUTPUTS):
            raise AccessError(self.env._('Allocation values and snapshots are server controlled.'))
        candidates = self.env['supplier.shortage.receipt.exception'].browse([
            values.get('detection_id',self.env.context.get('default_detection_id')) for values in vals_list
            if values.get('detection_id',self.env.context.get('default_detection_id'))])
        with self.env.cr.savepoint():
            if candidates:
                require_claim_user(self.env)
                candidates._lock_preparation()
                candidates._validate_current_candidate()
            lines = super().create(vals_list)
            allocated = lines.filtered('detection_id')
            from .shortage_claim_line import ShortageClaimLine
            for line in allocated:
                rec = line.detection_id
                if line.purchase_line_id!=rec.purchase_line_id or line.claim_id.purchase_id!=rec.purchase_id or line.company_id!=rec.company_id:
                    raise ValidationError(self.env._('Allocation must match the exact candidate company, PO and PO line.'))
                super(ShortageClaimLine,line).write({'allocated_qty':line._normalized_quantity(),'allocation_state':'active'})
            allocated._store_allocation_basis(initial=True)
            allocated._check_allocation_totals()
            candidates._touch_allocations()
            for claim in allocated.claim_id:
                claim._audit('allocation_created',self.env._('Candidate quantities reserved for draft preparation.'),
                    basis={claim.id:{str(l.id):l.preparation_snapshot for l in allocated.filtered(lambda row:row.claim_id==claim)}})
            return lines

    def write(self, vals):
        self.check_access('write')
        if LINE_OUTPUTS & vals.keys():
            raise AccessError(self.env._('Allocation state, quantities and snapshots are server controlled.'))
        if 'detection_id' in vals:
            raise ValidationError(self.env._('Candidate identity cannot be changed by editing. Prepare a new candidate-backed line; retain manual lines as incomplete history.'))
        backed = self.filtered('detection_id')
        if backed and vals.keys() & {'claim_id','purchase_line_id','product_id','claim_uom_id'}:
            raise ValidationError(self.env._('Allocated source identity/UoM is immutable. Release and prepare a new line.'))
        with self.env.cr.savepoint():
            lock_records(self.claim_id.purchase_id)
            if backed:
                backed.detection_id._lock_preparation()
            if backed and 'proposed_qty' in vals:
                require_claim_user(self.env)
                if any(line.allocation_state not in (False,'active') or len((line.claim_id.correction_reason or '').strip())<5 for line in backed):
                    raise ValidationError(self.env._('Only active draft allocations may be corrected, with a meaningful correction reason.'))
                for line in backed:
                    basis = line.allocation_basis or {}
                    if (basis.get('purchase_uom_id')!=line.detection_id.product_uom_id.id or
                        basis.get('purchase_uom_factor')!=line.detection_id.product_uom_id.factor or
                        basis.get('claim_uom_factor')!=line.claim_uom_id.factor):
                        raise ValidationError(self.env._('Captured UoM configuration changed; use an authorized release and corrected preparation.'))
                    new_qty = line.claim_uom_id._compute_quantity(vals['proposed_qty'],line.purchase_line_id.uom_id,round=False)
                    if line.purchase_line_id.uom_id.compare(new_qty,line.allocated_qty)>0:
                        line.detection_id._validate_current_candidate()
                        line._validate_allocation_basis()
            old_totals = {rec.id:sum(rec._allocation_lines().mapped('allocated_qty')) for rec in backed.detection_id}
            result = super().write(vals)
            if backed and 'proposed_qty' in vals:
                from .shortage_claim_line import ShortageClaimLine
                for line in backed:
                    super(ShortageClaimLine,line).write({'allocated_qty':line._normalized_quantity(),
                        'allocation_basis':dict(line.allocation_basis or {},allocated_qty=line._normalized_quantity())})
                for rec in backed.detection_id:
                    total = sum(rec._allocation_lines().mapped('allocated_qty'))
                    if rec.product_uom_id.compare(total,rec.candidate_qty)>0 and rec.product_uom_id.compare(total,old_totals[rec.id])>=0:
                        raise ValidationError(self.env._('An overallocated candidate permits reductions only until corrected.'))
                backed.detection_id._touch_allocations()
                backed.claim_id._audit('allocation_changed',self.env._('Draft reservation quantity corrected.'),
                    reason='; '.join(backed.claim_id.mapped('correction_reason')),basis={c.id:{str(l.id):l.allocated_qty for l in backed.filtered(lambda row:row.claim_id==c)} for c in backed.claim_id})
            return result

    def action_bind_reviewed_candidate(self, candidate_id):
        self.ensure_one()
        require_claim_user(self.env)
        self.check_access('write')
        candidate = self.env['supplier.shortage.receipt.exception'].browse(candidate_id)
        with self.env.cr.savepoint():
            candidate._lock_preparation()
            self.claim_id._lock_for_edit()
            self.lock_for_update()
            self.invalidate_recordset()
            if self.detection_id or self.allocation_state or self.claim_id.state!='draft' or len((self.claim_id.manual_exception_reason or '').strip())<5:
                raise ValidationError(self.env._('Link only an unallocated manual draft line with a documented manual preparation reason.'))
            candidate._validate_current_candidate()
            if self.purchase_line_id!=candidate.purchase_line_id or self.claim_id.purchase_id!=candidate.purchase_id or self.company_id!=candidate.company_id:
                raise ValidationError(self.env._('The reviewed candidate must match this exact claim company, PO and PO line.'))
            from .shortage_claim_line import ShortageClaimLine
            super(ShortageClaimLine,self).write({'detection_id':candidate.id,'allocated_qty':self._normalized_quantity(),'allocation_state':'active'})
            self._store_allocation_basis(initial=True)
            self._check_allocation_totals()
            candidate._touch_allocations()
            self.claim_id._audit('allocation_created',self.env._('Manual draft line linked to reviewed candidate %s.',candidate.name),
                reason=self.claim_id.manual_exception_reason,basis={self.claim_id.id:{str(self.id):self.preparation_snapshot}})
        return self.claim_id._preparation_action()

    def action_bind_candidate_dialog(self):
        self.ensure_one()
        self.check_access('write')
        candidate = self.env['supplier.shortage.receipt.exception'].search([
            ('reconciliation_kind','=','detection'),('purchase_line_id','=',self.purchase_line_id.id)])
        candidate._validate_current_candidate()
        if len(candidate)!=1:
            raise ValidationError(self.env._('Obtain one reviewed detection source for this purchase line first.'))
        wizard = self.env['supplier.shortage.prepare.wizard'].create({'claim_line_id':self.id,
            'line_ids':[Command.create({'detection_id':candidate.id,'quantity':self._normalized_quantity()})]})
        return {'type':'ir.actions.act_window','res_model':wizard._name,'res_id':wizard.id,'view_mode':'form','target':'new'}

    def action_release_allocation(self):
        require_claim_user(self.env,manager=True)
        with self.env.cr.savepoint():
            self.detection_id._lock_preparation()
            self.claim_id._lock_for_edit()
            if any(line.claim_id.state!='draft' or line.allocation_state not in (False,'active') or len((line.claim_id.correction_reason or '').strip())<5 for line in self):
                raise ValidationError(self.env._('Release only active draft reservations with an authorized correction reason.'))
            from .shortage_claim_line import ShortageClaimLine
            super(ShortageClaimLine,self).write({'allocation_state':'released'})
            self.detection_id._touch_allocations()
            self.claim_id._audit('allocation_released',self.env._('Draft reservation explicitly released; line, snapshot and evidence retained.'),
                reason='; '.join(self.claim_id.mapped('correction_reason')),basis={c.id:{str(l.id):l.allocated_qty for l in self.filtered(lambda row:row.claim_id==c)} for c in self.claim_id})
        return True

    def unlink(self):
        if self.filtered('detection_id') or self.env['supplier.shortage.claim.evidence'].search_count([('claim_line_id','in',self.ids)]):
            raise AccessError(self.env._('Allocation/evidence history cannot be deleted. Use an authorized reservation release.'))
        return super().unlink()

    def copy(self, default=None):
        return super().copy(default)


class PreparationWizard(models.TransientModel):
    _name = 'supplier.shortage.prepare.wizard'
    _description = 'Prepare Reviewed Shortage Claim'
    claim_line_id = fields.Many2one('supplier.shortage.claim.line')
    line_ids = fields.One2many('supplier.shortage.prepare.wizard.line','wizard_id')

    def action_prepare(self):
        self.ensure_one()
        self.check_access('read')
        candidates = self.line_ids.detection_id
        if not candidates or len(candidates)!=len(self.line_ids):
            raise ValidationError(self.env._('Select distinct eligible candidates.'))
        if self.claim_line_id:
            if len(candidates)!=1 or candidates.product_uom_id.compare(self.claim_line_id._normalized_quantity(),self.line_ids.quantity)!=0:
                raise ValidationError(self.env._('For manual binding, correct the line quantity explicitly before linking.'))
            return self.claim_line_id.action_bind_reviewed_candidate(candidates.id)
        return candidates.action_prepare_shortage_claim({str(row.detection_id.id):row.quantity for row in self.line_ids})


class PreparationWizardLine(models.TransientModel):
    _name = 'supplier.shortage.prepare.wizard.line'
    _description = 'Claim Preparation Quantity'
    wizard_id = fields.Many2one('supplier.shortage.prepare.wizard',required=True,ondelete='cascade')
    detection_id = fields.Many2one('supplier.shortage.receipt.exception',required=True)
    quantity = fields.Float(required=True,digits='Product Unit')
    available_qty = fields.Float(related='detection_id.available_qty',compute_sudo=False)
    product_uom_id = fields.Many2one(related='detection_id.product_uom_id',compute_sudo=False)


class PreparationPolicy(models.Model):
    _inherit = 'res.company'
    ssc_min_verified_evidence = fields.Integer(string='Minimum Verified Evidence per Claim Line',default=1)
    ssc_evidence_max_mb = fields.Integer(string='Maximum Evidence File Size (MB)',default=10)
    ssc_evidence_extensions = fields.Char(string='Allowed Evidence Extensions',default='pdf,png,jpg,jpeg,txt,csv')

    @api.constrains('ssc_min_verified_evidence','ssc_evidence_max_mb','ssc_evidence_extensions')
    def _check_preparation_policy(self):
        if any(c.ssc_min_verified_evidence<1 or c.ssc_evidence_max_mb<1 or not c.ssc_evidence_extensions for c in self):
            raise ValidationError(self.env._('Use at least one verified evidence record, a positive file size limit and allowed extensions.'))


class PurchasePreparationSources(models.Model):
    _inherit = 'purchase.order.line'
    shortage_review_exception_ids = fields.One2many('supplier.shortage.receipt.exception','purchase_line_id',readonly=True)

class EvidencePolicyWizard(models.TransientModel):
    _name = 'supplier.shortage.evidence.policy.wizard'
    _description = 'Company Claim Evidence Policy'
    _allow_sudo_commands = False

    company_id = fields.Many2one('res.company',required=True,default=lambda self:self.env.company)
    minimum_verified = fields.Integer(required=True,default=lambda self:self.env.company.ssc_min_verified_evidence)
    maximum_mb = fields.Integer(required=True,default=lambda self:self.env.company.ssc_evidence_max_mb)
    extensions = fields.Char(required=True,default=lambda self:self.env.company.ssc_evidence_extensions)

    @api.onchange('company_id')
    def _onchange_company(self):
        if self.company_id and self.company_id in self.env.companies:
            self.minimum_verified=self.company_id.ssc_min_verified_evidence
            self.maximum_mb=self.company_id.ssc_evidence_max_mb
            self.extensions=self.company_id.ssc_evidence_extensions

    def action_apply(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group(ROLE+'group_module_admin') or self.company_id not in self.env.companies:
            raise AccessError(self.env._('Only a recovery configuration administrator may update policy for an activated authorized company.'))
        self.company_id.check_access('read')
        values={'ssc_min_verified_evidence':self.minimum_verified,
                'ssc_evidence_max_mb':self.maximum_mb,'ssc_evidence_extensions':self.extensions}
        if self.minimum_verified<1 or not 1<=self.maximum_mb<=100 or not (self.extensions or '').strip():
            raise ValidationError(self.env._('Require at least one verified item, a 1-100 MB file limit and allowed extensions.'))
        # Narrow configuration-only write boundary after explicit role/company checks.
        # No source reads are elevated, no business permission is granted, and callers
        # cannot supply a field name or change company identity/accounting settings.
        self.company_id.sudo().with_context({}).write(values)
        return {'type':'ir.actions.act_window_close'}
