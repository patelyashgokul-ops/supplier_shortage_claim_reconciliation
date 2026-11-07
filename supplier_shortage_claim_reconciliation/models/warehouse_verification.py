import math
from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, ValidationError
from .claim_preparation import ROLE, lock_records
from .evidence_verification import digest
from .source_evidence_invalidation import invalidate_lines


class WarehouseObservation(models.Model):
    _name='supplier.shortage.warehouse.observation'
    _description='Delivery-Specific Warehouse Verification'
    _check_company_auto=True
    _allow_sudo_commands=False
    _rec_name='explanation'

    claim_line_id=fields.Many2one('supplier.shortage.claim.line',required=True,check_company=True,ondelete='restrict',index=True)
    claim_id=fields.Many2one(related='claim_line_id.claim_id',store=True,readonly=True)
    company_id=fields.Many2one(related='claim_line_id.company_id',store=True,readonly=True,index=True)
    product_id=fields.Many2one(related='claim_line_id.product_id',compute_sudo=False)
    warehouse_id=fields.Many2one('stock.warehouse',required=True,check_company=True)
    receipt_id=fields.Many2one('stock.picking',check_company=True,ondelete='restrict')
    location_id=fields.Many2one('stock.location',check_company=True,ondelete='restrict')
    observation_type=fields.Selection([('delivery_count','Total Verified Delivery Quantity'),
        ('additional_unrecorded','Additional Unrecorded Units Found')],required=True,default='delivery_count')
    observed_qty=fields.Float(required=True,digits='Product Unit')
    uom_id=fields.Many2one('uom.uom',required=True,ondelete='restrict')
    observed_at=fields.Datetime(required=True,default=fields.Datetime.now)
    count_method=fields.Selection([('physical','Delivery-Specific Physical Count'),('documents','Receiving Document Reconciliation')],required=True)
    explanation=fields.Text(required=True)
    evidence_ids=fields.Many2many('supplier.shortage.claim.evidence','ssc_observation_evidence_rel','observation_id','evidence_id',check_company=True)
    recorded_by=fields.Many2one('res.users',readonly=True,default=lambda self:self.env.user,ondelete='restrict')
    verification_state=fields.Selection([('not_reviewed','Not Reviewed'),('verified','Verified'),('rejected','Rejected')],readonly=True,default='not_reviewed')
    verified_by=fields.Many2one('res.users',readonly=True,ondelete='restrict')
    verified_at=fields.Datetime(readonly=True)
    verification_note=fields.Text()
    captured_basis=fields.Json(readonly=True)
    reviewed_fingerprint=fields.Char(readonly=True)
    is_stale=fields.Boolean(readonly=True,default=False)
    is_current=fields.Boolean(readonly=True,default=True)
    current_line_key=fields.Char(readonly=True,index=True,copy=False)
    _current_line_unique=models.Constraint('UNIQUE(current_line_key)','Replace the current warehouse conclusion explicitly before recording another.')
    supersedes_id=fields.Many2one('supplier.shortage.warehouse.observation',readonly=True,ondelete='restrict')
    effective_verified=fields.Boolean(compute='_compute_position')
    source_conflict=fields.Boolean(compute='_compute_position')
    indicative_supported_qty=fields.Float(compute='_compute_position',digits=0,aggregator=False,
        string='Indicative Physical Difference - Requires Source Reconciliation')

    def _access_domain(self, operation):
        return super()._access_domain(operation) & Domain('claim_line_id','access','read')

    def _basis(self):
        self.ensure_one()
        line=self.claim_line_id
        return {'purchase_line_id':line.purchase_line_id.id,'source_signature':line.detection_id.source_signature,
            'allocated_qty':line.allocated_qty,'claim_qty':line.proposed_qty,'claim_uom_factor':line.claim_uom_id.factor,
            'quantity':self.observed_qty,'uom':self.uom_id.id,'factor':self.uom_id.factor,
            'type':self.observation_type,'receipt':self.receipt_id.id,'warehouse':self.warehouse_id.id,
            'native_net':line.detection_id.net_received_qty,'candidate_qty':line.detection_id.candidate_qty,
            'evidence':[[e.id,e.is_current,e.reviewed_fingerprint,e.is_stale] for e in self.evidence_ids.sorted('id')]}

    @api.depends('verification_state','is_stale','is_current','captured_basis','claim_line_id.detection_id.source_signature',
                 'claim_line_id.proposed_qty','evidence_ids.is_current','evidence_ids.is_stale')
    def _compute_position(self):
        for rec in self:
            line=rec.claim_line_id
            candidate=line.detection_id
            qty=rec.uom_id._compute_quantity(rec.observed_qty,line.purchase_line_id.uom_id,round=False) if rec.uom_id and line.purchase_line_id.uom_id else 0
            preview=max(candidate.candidate_qty-qty,0) if rec.observation_type=='additional_unrecorded' else max(candidate.ordered_qty-qty,0)
            rec.indicative_supported_qty=min(candidate.candidate_qty,preview)
            rec.source_conflict=bool(candidate.product_uom_id and (candidate.product_uom_id.compare(qty,0)>0 if rec.observation_type=='additional_unrecorded'
                else candidate.product_uom_id.compare(qty,candidate.net_received_qty)!=0))
            rec.effective_verified=(rec.verification_state=='verified' and rec.is_current and not rec.is_stale
                and rec.verified_by!=rec.recorded_by and rec.reviewed_fingerprint==digest(rec._basis())
                and all(e.effective_state=='verified' for e in rec.evidence_ids))

    @api.model_create_multi
    def create(self, vals_list):
        if not any(self.env.user.has_group(ROLE+role) for role in ('group_claim_user','group_warehouse_user')):
            raise AccessError(self.env._('Only authorized claim or warehouse contributors may record observations.'))
        protected={'company_id','claim_id','product_id','recorded_by','verified_by','verified_at','verification_state',
                   'captured_basis','reviewed_fingerprint','is_stale','is_current','supersedes_id','current_line_key','correction_reason'}
        if any(protected & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Observation provenance and verification outputs are protected.'))
        with self.env.cr.savepoint():
            prepared=[]
            for values in vals_list:
                vals=dict(values)
                line=self.env['supplier.shortage.claim.line'].browse(vals.get('claim_line_id'))
                line.check_access('read')
                line.claim_id.check_access('read')
                lock_records(line.claim_id.purchase_id)
                lock_records(line.claim_id)
                if line.claim_id.state!='draft' or not line.detection_id or line.allocation_state!='active':
                    raise ValidationError(self.env._('Record observations only against active allocated draft lines.'))
                if len((vals.get('explanation') or '').strip())<15:
                    raise ValidationError(self.env._('Explain the delivery-specific count method and provenance.'))
                vals.update(recorded_by=self.env.uid,verification_state='not_reviewed',verified_by=False,verified_at=False,
                    captured_basis=False,reviewed_fingerprint=False,is_stale=False,is_current=True,supersedes_id=False,current_line_key=str(line.id))
                prepared.append(vals)
            records=super(WarehouseObservation,self.with_context({k:v for k,v in self.env.context.items() if not k.startswith('default_')})).create(prepared)
            records._check_observation()
            for rec in records:
                rec._audit('warehouse_recorded')
            records.claim_id._invalidate_evidence_readiness('Warehouse observation requires independent verification.')
            return records

    @api.constrains('claim_line_id','warehouse_id','receipt_id','location_id','observed_qty','uom_id','evidence_ids')
    def _check_observation(self):
        for rec in self:
            line=rec.claim_line_id
            (rec.warehouse_id|rec.receipt_id.picking_type_id.warehouse_id).check_access('read')
            if not math.isfinite(rec.observed_qty) or rec.observed_qty<0 or not rec.uom_id._has_common_reference(line.purchase_line_id.uom_id):
                raise ValidationError(self.env._('Use a nonnegative finite delivery quantity and compatible UoM.'))
            if rec.warehouse_id!=line.claim_id.purchase_id.picking_type_id.warehouse_id or rec.warehouse_id.company_id!=rec.company_id:
                raise ValidationError(self.env._('Observation warehouse must match the source PO warehouse and company.'))
            if rec.receipt_id:
                rec.receipt_id.check_access('read')
                if rec.receipt_id not in line.detection_id.source_move_ids.picking_id:
                    raise ValidationError(self.env._('The receipt is unrelated to this exact purchase line.'))
            if rec.location_id:
                rec.location_id.check_access('read')
                if rec.location_id not in (line.detection_id.source_move_ids.location_id|line.detection_id.source_move_ids.location_dest_id):
                    raise ValidationError(self.env._('Use a location from the delivery provenance; company-wide on-hand stock is not delivery evidence.'))
            rec.evidence_ids.check_access('read')
            if any(e.claim_id!=rec.claim_id or line not in e._scope_lines() for e in rec.evidence_ids):
                raise ValidationError(self.env._('Supporting documents must explicitly cover this claim line.'))

    def write(self, vals):
        if vals.keys()-{'verification_note'}:
            raise AccessError(self.env._('Observations are immutable. Record an audited replacement observation.'))
        self.check_access('write')
        lock_records(self.claim_id.purchase_id)
        lock_records(self.claim_id)
        if any(rec.claim_id.state!='draft' or not rec.is_current for rec in self):
            raise ValidationError(self.env._('Only current draft observations accept review notes.'))
        return super().write(vals)

    def _audit(self,event):
        self.ensure_one()
        self.env['supplier.shortage.claim.audit']._append_warehouse(self,event)

    def action_verify(self):
        return self._review('verified')

    def action_reject(self):
        return self._review('rejected')

    def _review(self,target):
        if not self.env.user.has_group(ROLE+'group_warehouse_reviewer'):
            raise AccessError(self.env._('An authorized warehouse reviewer is required.'))
        self.check_access('write')
        with self.env.cr.savepoint():
            lock_records(self.claim_id.purchase_id)
            lock_records(self.claim_id)
            lock_records(self)
            self.invalidate_recordset()
            if any(rec.recorded_by==self.env.user for rec in self):
                raise AccessError(self.env._('An independent checker must review the warehouse observation.'))
            if any(rec.claim_id.state!='draft' or not rec.is_current or len((rec.verification_note or '').strip())<10 for rec in self):
                raise ValidationError(self.env._('Review current draft observations with a substantive conclusion.'))
            self._check_observation()
            for rec in self:
                basis=rec._basis()
                super(WarehouseObservation,rec).write({'verification_state':target,'verified_by':self.env.uid,
                    'verified_at':fields.Datetime.now(),'reviewed_fingerprint':digest(basis),'captured_basis':basis,'is_stale':False})
                rec._audit('warehouse_reviewed')
            # Do not invalidate the observation just reviewed. Evidence/claim readiness changes only.
            self.claim_id._invalidate_evidence_readiness('Warehouse conclusion changed; evaluate source consistency.')
        return True

    def action_prepare_correction(self):
        self.ensure_one()
        self.check_access('write')
        if not self.is_current or self.claim_id.state!='draft':
            raise ValidationError(self.env._('Correct only the current observation on a draft claim.'))
        wizard=self.env['supplier.shortage.warehouse.revision.wizard'].create({
            'observation_id':self.id,'observed_qty':self.observed_qty,'uom_id':self.uom_id.id,
            'count_method':self.count_method,'explanation':self.explanation})
        return {'type':'ir.actions.act_window','res_model':wizard._name,'res_id':wizard.id,
            'view_mode':'form','views':[(False,'form')],'target':'new'}

    correction_reason=fields.Text(readonly=True,copy=False)

    def action_replace(self, values, reason):
        self.ensure_one()
        with self.env.cr.savepoint():
            self.check_access('write')
            lock_records(self.claim_id.purchase_id)
            lock_records(self.claim_id)
            lock_records(self)
            if not self.is_current or self.claim_id.state!='draft' or len((reason or '').strip())<10:
                raise ValidationError(self.env._('Replace a current draft observation with a meaningful correction reason.'))
            allowed={'observed_qty','uom_id','observation_type','count_method','explanation','evidence_ids','observed_at'}
            if values.keys()-allowed: raise AccessError(self.env._('Unsupported observation revision fields.'))
            vals={key:self[key].id if self._fields[key].type=='many2one' else self[key] for key in
                ('claim_line_id','warehouse_id','receipt_id','location_id','observed_qty','uom_id','observation_type','count_method','explanation','observed_at')}
            vals.update(values)
            super(WarehouseObservation,self).write({'is_current':False,'current_line_key':False})
            replacement=self.create(vals)
            super(WarehouseObservation,replacement).write({'supersedes_id':self.id,'correction_reason':reason})
            replacement._audit('warehouse_revised')
            return {'type':'ir.actions.act_window','res_model':self._name,'res_id':replacement.id,'view_mode':'form','views':[(False,'form')]}

    def unlink(self):
        raise AccessError(self.env._('Warehouse observation history cannot be deleted.'))

    def copy(self,default=None):
        raise AccessError(self.env._('Use a reasoned replacement observation.'))


class WarehouseRevisionWizard(models.TransientModel):
    _name='supplier.shortage.warehouse.revision.wizard'
    _description='Correct Warehouse Observation Without Erasing History'
    observation_id=fields.Many2one('supplier.shortage.warehouse.observation',required=True)
    observed_qty=fields.Float(required=True)
    uom_id=fields.Many2one('uom.uom',required=True)
    count_method=fields.Selection(selection=lambda self:self.env['supplier.shortage.warehouse.observation']._fields['count_method']._description_selection(self.env),required=True)
    explanation=fields.Text(required=True)
    change_reason=fields.Text()

    def action_create_correction(self):
        self.ensure_one()
        self.check_access('read')
        original=self.observation_id
        original.check_access('write')
        return original.action_replace({'observed_qty':self.observed_qty,'uom_id':self.uom_id.id,
            'count_method':self.count_method,'explanation':self.explanation,
            'evidence_ids':[(6,0,original.evidence_ids.ids)]},self.change_reason)
