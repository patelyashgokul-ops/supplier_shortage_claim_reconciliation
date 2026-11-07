from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from markupsafe import Markup, escape
from .claim_preparation import ROLE, lock_records, require_claim_user
from .claim_evidence import ClaimEvidence
from .evidence_verification import digest, PHYSICAL, FINANCIAL
from .receipt_analysis import _coverage_search
from .source_evidence_invalidation import invalidate_lines, invalidate_policy

STATES=[('incomplete','Incomplete'),('needs_verification','Needs Verification'),('correction','Needs Correction'),
        ('blocked','Blocked by Source Issue'),('ready','Ready for Internal Review')]
SCENARIOS=[('billed_unreceived','Billed but Unreceived'),('backorder_dispute','Backorder / Delivery Dispute'),
           ('warehouse_error','Warehouse Recording Error'),('quality_return','Return / Quality Issue')]
CATEGORIES=[('purchase','Purchase Reference'),('receiving','Receiving Provenance'),('billing','Accounting Evidence'),
            ('warehouse','Warehouse Verification'),('communication','Supplier Communication'),('quality','Quality Evidence'),('support','Supporting Document')]
HEAD_OUTPUTS={'readiness_status','readiness_dirty','readiness_message','readiness_fingerprint','readiness_checked_at','readiness_checklist','missing_evidence_count','readiness_access_complete'}
LINE_OUTPUTS={'readiness_status','readiness_message','readiness_checks','readiness_fingerprint','supported_qty'}


class EvidenceReadiness(models.Model):
    _inherit='supplier.shortage.claim'

    evidence_scenario=fields.Selection(SCENARIOS,required=True,default='billed_unreceived')
    readiness_status=fields.Selection(STATES,readonly=True,default='incomplete',copy=False,index=True)
    readiness_dirty=fields.Boolean(readonly=True,default=True,copy=False)
    readiness_message=fields.Text(readonly=True,default='Evaluate current sources and supporting evidence.',copy=False)
    readiness_fingerprint=fields.Char(readonly=True,copy=False)
    readiness_checked_at=fields.Datetime(readonly=True,copy=False)
    readiness_checklist=fields.Html(compute='_compute_checklist',sanitize=True)
    missing_evidence_count=fields.Integer(compute='_compute_checklist')
    readiness_access_complete=fields.Boolean(compute='_compute_checklist')
    warehouse_observation_ids=fields.One2many('supplier.shortage.warehouse.observation','claim_id',readonly=True)
    evidence_exception_ids=fields.One2many('supplier.shortage.evidence.exception','claim_id',copy=False)

    def _auto_init(self):
        result=super()._auto_init()
        self.env.registry._ssc_evidence_schema_ready=True
        return result

    @api.model_create_multi
    def create(self, vals_list):
        if any(HEAD_OUTPUTS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Readiness results are server controlled.'))
        return super().create([dict(vals,readiness_status='incomplete',readiness_dirty=True,readiness_message='Evaluate current evidence.',
            readiness_fingerprint=False,readiness_checked_at=False) for vals in vals_list])

    def write(self,vals):
        if HEAD_OUTPUTS & vals.keys():
            raise AccessError(self.env._('Readiness results cannot be written directly.'))
        if 'evidence_scenario' in vals:
            self._lock_for_edit()
            if any(c.state!='draft' for c in self): raise ValidationError(self.env._('Return to draft before changing the evidence situation.'))
        result=super().write(vals)
        if vals.keys() & {'evidence_scenario','shortage_reason','purchase_id','partner_id'}:
            self._invalidate_evidence_readiness('Claim preparation context changed.')
        return result

    def _invalidate_evidence_readiness(self,reason):
        # Private metadata-only notification; source callers have already checked
        # parent/evidence authorization. No elevated document reads are performed.
        if not self: return
        self.flush_recordset(['readiness_dirty'])
        self.env.cr.execute("UPDATE supplier_shortage_claim SET readiness_dirty=TRUE,readiness_status='incomplete',readiness_message=%s WHERE id IN %s",[reason,tuple(self.ids)])
        self.invalidate_recordset(['readiness_dirty','readiness_status','readiness_message','readiness_checklist'])

    @api.depends('claim_line_ids.readiness_status','claim_line_ids.readiness_message','readiness_dirty')
    @api.depends_context('uid','allowed_company_ids')
    def _compute_checklist(self):
        for claim in self:
            blocked=_coverage_search(claim,[('id','=',claim.id),('claim_line_ids','any!',['!',('id','access','read')])]) if claim.id else claim.browse()
            claim.readiness_access_complete=not bool(blocked)
            if blocked:
                claim.readiness_checklist=self.env._('Complete source access is required to view line-level readiness. No hidden document counts are shown.')
                claim.missing_evidence_count=0
                continue
            rows=[]
            missing=0
            for number,line in enumerate(claim._review_lines(),1):
                checks=line.readiness_checks or []
                missing+=sum(not row['ok'] for row in checks)
                rows.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td></tr>')%(number,line.product_id.display_name,line.readiness_message or 'Not evaluated'))
            claim.missing_evidence_count=missing
            claim.readiness_checklist=Markup('<table class="table table-sm"><thead><tr><th>Line</th><th>Product</th><th>Required Actions</th></tr></thead><tbody>%s</tbody></table>')%Markup('').join(rows)

    def _evaluate_evidence_readiness(self):
        self.check_access('write')
        self._lock_for_edit()
        lines=self._review_lines()
        if _coverage_search(self,[('id','in',self.ids),('claim_line_ids','any!',['!',('id','access','read')])]):
            raise AccessError(self.env._('Claim lines or their native sources are inaccessible; readiness cannot be established.'))
        purchases=lines.purchase_line_id
        receiving=purchases._ssc_receiving_analysis()
        billing=purchases._ssc_billing_analysis()
        candidates=lines.detection_id
        lock_records(candidates)
        active=candidates._allocation_lines() if candidates else self.env['supplier.shortage.claim.line']
        totals={rec.id:sum(active.filtered(lambda row:row.detection_id==rec).mapped('allocated_qty')) for rec in candidates}
        critical=self.env['supplier.shortage.receipt.exception'].search([('purchase_line_id','in',purchases.ids),
            ('reconciliation_kind','!=','detection'),('condition_present','=',True),('severity','=','high'),('review_state','in',['new','in_review'])])
        if _coverage_search(purchases,[('id','in',purchases.ids),('shortage_review_exception_ids','any!',['!',('id','access','read')])]):
            raise AccessError(self.env._('Critical source review visibility is incomplete.'))
        observations=self.env['supplier.shortage.warehouse.observation'].search([('claim_line_id','in',lines.ids),('is_current','=',True)])
        rules=self.env['supplier.shortage.evidence.requirement'].search([('company_id','in',self.company_id.ids),('active','=',True)])
        exceptions=self.env['supplier.shortage.evidence.exception'].search([('claim_id','in',self.ids),('state','=','approved')])
        for claim in self:
            evidence=claim.evidence_ids
            if _coverage_search(claim,[('id','=',claim.id),('evidence_ids','any!',['!',('id','access','read')])]):
                raise AccessError(self.env._('Evidence visibility is incomplete; hidden records are not interpreted as missing.'))
            evidence.check_access('read')
            lock_records(evidence)
            states=[]; fingerprints=[]
            for number,line in enumerate(claim._review_lines(),1):
                checks=[]; messages=[]; conditions=[]
                def check(key,ok,message,state='incomplete'):
                    checks.append({'key':key,'ok':bool(ok),'message':message})
                    if not ok: messages.append(message);conditions.append(state)
                rec=line.detection_id; po=line.purchase_line_id
                r=receiving.get(po.id,{}); b=billing.get(po.id,{})
                source_ok=bool(po and rec and rec.purchase_line_id==po and rec.purchase_id==claim.purchase_id and rec.company_id==claim.company_id
                    and rec.partner_id==claim.partner_id and po.company_id==claim.company_id and po.order_id.state=='purchase'
                    and rec.review_state=='resolved' and rec.classification=='shortage_candidate_review' and rec.evidence_complete
                    and rec.product_uom_id==po.uom_id and r.get('accessible') and b.get('accessible') and r.get('complete') and b.get('complete'))
                if source_ok:
                    detail=po._ssc_receiving_integrity(r)
                    source_ok=not detail['native_conflict'] and po._ssc_detection_source_signature(r,b,detail)==rec.source_signature
                check('source',source_ok,'Current reviewed purchase, receipt and bill sources are required.','blocked')
                quantity_ok=bool(rec and line.allocation_state=='active' and po.uom_id.compare(line.allocated_qty,0)>0 and not rec.allocation_integrity_blocked
                    and po.uom_id.compare(totals.get(rec.id,0),rec.candidate_qty)<=0)
                try: line._validate_allocation_basis()
                except (ValidationError,AccessError): quantity_ok=False
                check('allocation',quantity_ok,'Reconcile the candidate fingerprint, captured UoM and reserved quantity.','blocked')
                check('critical',not critical.filtered(lambda item:item.purchase_line_id==po),'Resolve critical receiving/accounting source findings.','blocked')
                scoped=evidence.filtered(lambda e:e.is_current and line in e._scope_lines())
                verified=scoped.filtered(lambda e:e.effective_state=='verified')
                categories={'purchase':True,'receiving':bool(r.get('complete')),
                    'billing':bool(verified.filtered(lambda e:(e.source_bill_id and e.source_bill_id.state=='posted' and e.source_bill_id.move_type=='in_invoice')
                        or (e.source_bill_line_id and e.source_bill_line_id.move_id.state=='posted' and e.source_bill_line_id.move_id.move_type=='in_invoice')
                        or (e.evidence_type=='account_note' and e.statement and b.get('bill_posted',0)>0))),
                    'communication':bool(verified.filtered(lambda e:e.evidence_type in {'supplier','supplier_confirmation','supplier_response'})),
                    'quality':bool(verified.filtered(lambda e:e.evidence_type=='quality')),'support':bool(verified)}
                physical=observations.filtered(lambda o:o.claim_line_id==line)
                accepted=physical.filtered('effective_verified')
                categories['warehouse']=bool(accepted)
                supported=rec.candidate_qty if rec else 0
                if accepted:
                    supported=min([supported]+accepted.mapped('indicative_supported_qty'))
                contradiction=bool(accepted.filtered('source_conflict'))
                check('warehouse_consistency',not contradiction,'Warehouse observation contradicts native receipts. Correct sources through authorized native workflows; no stock or allocation was changed.','blocked')
                if accepted and po.uom_id.compare(line.allocated_qty,supported)>0:
                    check('supported_quantity',False,'Blocking allocation integrity exception: reservation exceeds the indicative warehouse-supported difference.','blocked')
                required={'purchase','receiving','warehouse'}
                if claim.evidence_scenario=='billed_unreceived': required.add('billing')
                if claim.evidence_scenario=='quality_return': required.add('quality')
                if claim.evidence_scenario=='backorder_dispute': required.add('communication')
                for rule in rules.filtered(lambda row:row.company_id==claim.company_id and row.scenario==claim.evidence_scenario):
                    waived=exceptions.filtered(lambda item:item.claim_line_id==line and item.requirement_id==rule and item.source_signature==rec.source_signature)
                    if rule.category not in {'purchase','receiving','billing','warehouse'} and rule.allow_exception and waived:
                        continue
                    required.add(rule.category)
                for category in sorted(required):
                    ok=categories.get(category,False)
                    relevant=scoped.filtered(lambda e:(e.evidence_type in FINANCIAL if category=='billing' else e.evidence_type in PHYSICAL if category=='warehouse' else True))
                    state='correction' if relevant.filtered(lambda e:e.effective_state=='rejected') else 'needs_verification' if relevant else 'incomplete'
                    check(category,ok,'%s verification is required for Line %s.'%(dict(CATEGORIES)[category],number),state)
                # Same source or binary cannot satisfy multiple independent proof counts.
                keys=set()
                for item in verified:
                    keys.add(('statement',digest(item.statement)) if item.statement else ('file',item.attachment_id.checksum) if item.attachment_id else tuple((field,item[field].id) for field in ('source_purchase_id','source_purchase_line_id','source_picking_id','source_move_id','source_bill_id','source_bill_line_id') if item[field]))
                check('minimum',len(keys)>=claim.company_id.ssc_min_verified_evidence,'Collect the configured minimum of distinct, current verified documents.')
                check('reason',len((claim.shortage_reason or '').strip())>=5,'Complete the shortage explanation.')
                for item in scoped:
                    if item.effective_state in {'not_reviewed','under_review','requires_reverification'}:
                        check('stale_document',False,'Document %s requires independent verification or correction.'%item.id,'needs_verification')
                ranking=['blocked','correction','incomplete','needs_verification']
                state=next((status for status in ranking if status in conditions),'ready')
                fp=digest([checks,[(e.id,e.effective_state,e.reviewed_fingerprint,e.is_current) for e in scoped],
                    [(o.id,o.reviewed_fingerprint,o.is_stale) for o in physical],rec.source_signature if rec else False,line.allocated_qty,supported])
                from .shortage_claim_line import ShortageClaimLine
                super(ShortageClaimLine,line).write({'readiness_status':state,'readiness_message':' '.join(messages) or 'All mandatory line requirements verified.',
                    'readiness_checks':checks,'readiness_fingerprint':fp,'supported_qty':supported})
                fingerprints.append([line.id,fp]);states.append(state)
            rank=['blocked','correction','incomplete','needs_verification']
            status=next((item for item in rank if item in states),'ready') if states else 'incomplete'
            signature=digest([status,fingerprints,claim.evidence_scenario])
            changed=signature!=claim.readiness_fingerprint
            from .shortage_claim import ShortageClaim
            super(ShortageClaim,claim).write({'readiness_status':status,'readiness_dirty':False,
                'readiness_message':'All active lines are ready for internal review.' if status=='ready' else 'Open the authorized line checklist and correct blocking or missing requirements.',
                'readiness_fingerprint':signature,'readiness_checked_at':fields.Datetime.now()})
            if changed:
                claim._audit('readiness_evaluated','Claim evidence readiness evaluated: %s.'%status,basis={claim.id:{'status':status,'line_fingerprints':fingerprints}})
        return all(claim.readiness_status=='ready' for claim in self)

    def action_evaluate_readiness(self):
        require_claim_user(self.env)
        with self.env.cr.savepoint():
            self._evaluate_evidence_readiness()
        return {'type':'ir.actions.client','tag':'display_notification','params':{'title':self.env._('Readiness Evaluated'),
            'message':self.env._('Review mandatory checks and line-specific actions. This is not final claim approval.'),'sticky':True}}

    def _validate_for_review(self):
        super()._validate_for_review()
        if not self._evaluate_evidence_readiness():
            details=' '.join(line.readiness_message for line in self._review_lines() if line.readiness_status!='ready')
            raise ValidationError(self.env._('Claim evidence is not ready for internal review. %s',details))


class EvidenceReadinessLine(models.Model):
    _inherit='supplier.shortage.claim.line'
    readiness_status=fields.Selection(STATES,readonly=True,default='incomplete',copy=False)
    readiness_message=fields.Text(readonly=True,copy=False)
    readiness_checks=fields.Json(readonly=True,copy=False)
    readiness_fingerprint=fields.Char(readonly=True,copy=False)
    supported_qty=fields.Float(readonly=True,digits=0,copy=False,aggregator=False,string='Indicative Supported Difference')

    @api.model_create_multi
    def create(self, vals_list):
        if any(LINE_OUTPUTS & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Line readiness is server controlled.'))
        return super().create([dict(vals,readiness_status='incomplete',readiness_message=False,
            readiness_checks=False,readiness_fingerprint=False,supported_qty=0) for vals in vals_list])

    def write(self,vals):
        if LINE_OUTPUTS & vals.keys(): raise AccessError(self.env._('Line readiness is protected.'))
        return super().write(vals)


class EvidenceRequirement(models.Model):
    _name='supplier.shortage.evidence.requirement'
    _description='Additional Company Evidence Requirement'
    _check_company_auto=True
    name=fields.Char(required=True)
    company_id=fields.Many2one('res.company',required=True,default=lambda self:self.env.company,index=True)
    scenario=fields.Selection(SCENARIOS,required=True)
    category=fields.Selection(CATEGORIES,required=True)
    active=fields.Boolean(default=True)
    allow_exception=fields.Boolean()
    _rule_unique=models.Constraint('UNIQUE(company_id,scenario,category)','This requirement already exists.')

    @api.model_create_multi
    def create(self,vals_list):
        self._require_admin()
        if any(vals.get('company_id',self.env.company.id) not in self.env.companies.ids for vals in vals_list):
            raise AccessError(self.env._('Configure only activated authorized companies.'))
        records=super().create(vals_list)
        records._dirty_policy_claims()
        return records
    def _require_admin(self):
        if not self.env.user.has_group(ROLE+'group_module_admin'):
            raise AccessError(self.env._('Evidence requirements are maintained by configuration administrators.'))
    def write(self,vals):
        self._require_admin()
        if {'company_id','category','scenario'} & vals.keys(): raise AccessError(self.env._('Requirement company identity is immutable.'))
        result=super().write(vals)
        self._dirty_policy_claims()
        return result
    def unlink(self):
        raise AccessError(self.env._('Archive requirement policies to retain exception history.'))
    def _dirty_policy_claims(self):
        for rule in self:
            invalidate_policy(self.env,rule.company_id.ids,rule.scenario)


class EvidenceException(models.Model):
    _name='supplier.shortage.evidence.exception'
    _description='Documented Additional-Evidence Policy Exception'
    _check_company_auto=True
    claim_id=fields.Many2one('supplier.shortage.claim',required=True,check_company=True,ondelete='restrict')
    claim_line_id=fields.Many2one('supplier.shortage.claim.line',required=True,check_company=True,ondelete='restrict')
    company_id=fields.Many2one(related='claim_id.company_id',store=True)
    requirement_id=fields.Many2one('supplier.shortage.evidence.requirement',required=True,check_company=True,ondelete='restrict')
    reason=fields.Text(required=True)
    state=fields.Selection([('requested','Requested'),('approved','Authorized Exception'),('rejected','Rejected')],readonly=True,default='requested')
    reviewed_by=fields.Many2one('res.users',readonly=True)
    reviewed_at=fields.Datetime(readonly=True)
    source_signature=fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if any({'state','reviewed_by','reviewed_at','source_signature','company_id'} & vals.keys() for vals in vals_list):
            raise AccessError(self.env._('Exception review fields are protected.'))
        records=super().create([dict(vals,state='requested',reviewed_by=False,reviewed_at=False,source_signature=False) for vals in vals_list])
        for rec in records:
            if rec.claim_line_id.claim_id!=rec.claim_id or rec.requirement_id.company_id!=rec.company_id or len(rec.reason.strip())<15:
                raise ValidationError(self.env._('Document a meaningful exception for the exact claim line and company policy.'))
        return records
    def write(self,vals):
        raise AccessError(self.env._('Policy exception history is immutable; use the review action.'))
    def action_authorize(self):
        require_claim_user(self.env,manager=True)
        self.check_access('write')
        self.claim_id._lock_for_edit()
        if any(rec.claim_id.state!='draft' or rec.state!='requested' or rec.create_uid==self.env.user or not rec.requirement_id.allow_exception
            or rec.requirement_id.category in {'purchase','receiving','billing','warehouse'} for rec in self):
            raise ValidationError(self.env._('Independent manager authorization is required; core source, accounting, warehouse and quantity gates cannot be waived.'))
        for rec in self:
            super(EvidenceException,rec).write({'state':'approved','reviewed_by':self.env.uid,'reviewed_at':fields.Datetime.now(),
                'source_signature':rec.claim_line_id.detection_id.source_signature})
            rec.claim_id._audit('evidence_exception','Additional-evidence exception authorized.',reason=rec.reason,
                basis={rec.claim_id.id:{'exception_id':rec.id,'line_id':rec.claim_line_id.id,'requirement_id':rec.requirement_id.id}})
        self.claim_id._invalidate_evidence_readiness('Evidence policy exception reviewed.')
        return True
    def unlink(self): raise AccessError(self.env._('Evidence-policy exception history is retained.'))
