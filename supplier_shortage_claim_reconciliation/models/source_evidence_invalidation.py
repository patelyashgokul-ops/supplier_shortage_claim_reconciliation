"""Bounded identity-only source notifications. They never update stock/accounting.

Native writers need not have claim access. SQL changes only private readiness/stale
metadata joined to the source FK, without reading or returning private documents.
It shares the source transaction and exposes no claim IDs or totals to the caller.
"""
from odoo import api, models

PHYSICAL=('receipt','challan','packing','count','photo','return','quality','warehouse_report','lot')
FINANCIAL=('bill','bill_line','credit','account_note')


def invalidate_lines(env, line_ids, reason, kind='all'):
    line_ids=tuple(set(line_ids))
    if not line_ids or not (env.registry.ready or getattr(env.registry,'_ssc_evidence_schema_ready',False)):
        return
    for model,names in [('supplier.shortage.claim.line',['claim_id','company_id','purchase_line_id']),
                        ('supplier.shortage.claim.evidence',['claim_id','scope_line_ids','is_current','is_stale']),
                        ('supplier.shortage.claim',['readiness_dirty'])]:
        env[model].flush_model(names)
    types=PHYSICAL if kind=='receiving' else FINANCIAL if kind=='billing' else ()
    clause='AND e.evidence_type IN %s' if types else 'AND FALSE' if kind=='candidate' else ''
    params=[line_ids]
    if types: params.append(types)
    params += [line_ids,line_ids,reason,env.uid,reason]
    env.cr.execute('''WITH marked AS (
        UPDATE supplier_shortage_claim_evidence e SET is_stale=TRUE
        WHERE e.is_current AND NOT e.is_stale AND (
            EXISTS (SELECT 1 FROM ssc_evidence_scope_rel r WHERE r.evidence_id=e.id AND r.line_id IN %s)
            OR e.claim_line_id IN %s
            OR (e.source_purchase_id IS NOT NULL AND EXISTS(SELECT 1 FROM supplier_shortage_claim_line l WHERE l.id IN %s AND l.claim_id=e.claim_id)))
        '''+clause+''' RETURNING e.id),
        changed AS (UPDATE supplier_shortage_claim c SET readiness_dirty=TRUE, readiness_status='blocked',readiness_message=%s
            WHERE NOT c.readiness_dirty AND EXISTS(SELECT 1 FROM supplier_shortage_claim_line l WHERE l.id IN %s AND l.claim_id=c.id)
            RETURNING c.id,c.company_id),
        audit AS (INSERT INTO supplier_shortage_claim_audit
            (claim_id,company_id,event_type,actor_id,event_datetime,change_summary)
            SELECT id,company_id,'readiness_invalidated',%s,statement_timestamp() AT TIME ZONE 'UTC',%s FROM changed RETURNING id)
        SELECT (SELECT count(*) FROM marked)+(SELECT count(*) FROM changed)''',
        [line_ids,line_ids,line_ids]+([types] if types else [])+[reason,line_ids,env.uid,reason])
    changed=env.cr.fetchone()[0]
    if kind!='billing':
        env.cr.execute('UPDATE supplier_shortage_warehouse_observation SET is_stale=TRUE WHERE claim_line_id IN %s AND is_current AND NOT is_stale',[line_ids])
        changed+=env.cr.rowcount
    env.cr.execute("UPDATE supplier_shortage_claim_line SET readiness_status='blocked',readiness_message=%s WHERE id IN %s",[reason,line_ids])
    changed+=env.cr.rowcount
    if changed:
        env['supplier.shortage.claim.evidence'].invalidate_model(['is_stale','effective_state'],flush=False)
        env['supplier.shortage.warehouse.observation'].invalidate_model(['is_stale','effective_verified','source_conflict','indicative_supported_qty'],flush=False)
        env['supplier.shortage.claim.line'].invalidate_model(['readiness_status','readiness_message'],flush=False)
        env['supplier.shortage.claim'].invalidate_model(['readiness_dirty','readiness_status','readiness_message','audit_event_ids','readiness_checklist','missing_evidence_count'],flush=False)
    if env.registry.ready or getattr(env.registry,'_ssc_approval_schema_ready',False):
        invalidate_approvals(env, line_ids, reason)


def invalidate_approvals(env, line_ids, reason):
    """Identity-only notification sharing the native source transaction/row locks."""
    env['supplier.shortage.claim'].flush_model(['approval_status'])
    env.cr.execute('''WITH changed AS (
        UPDATE supplier_shortage_claim c SET approval_status='stale'
        WHERE c.approval_status IN ('purchase','accounts','finance','approved')
          AND EXISTS (SELECT 1 FROM supplier_shortage_claim_line l WHERE l.id IN %s AND l.claim_id=c.id)
        RETURNING c.id,c.company_id)
        INSERT INTO supplier_shortage_claim_audit
        (claim_id,company_id,event_type,actor_id,event_datetime,change_summary,reason)
        SELECT id,company_id,'approval_event',%s,statement_timestamp() AT TIME ZONE 'UTC',
        'Material source change invalidated current internal authorization; historical decisions retained.',%s FROM changed''',
        [tuple(line_ids),env.uid,reason])
    env['supplier.shortage.claim'].invalidate_model(['approval_status','audit_event_ids'],flush=False)
    env['supplier.shortage.claim.approval'].invalidate_model(['claim_approval_status','can_decide'],flush=False)


def invalidate_po_lines(env, ids, kind):
    ids=tuple(set(ids))
    if not ids or not (env.registry.ready or getattr(env.registry,'_ssc_evidence_schema_ready',False)):
        return
    env['supplier.shortage.claim.line'].flush_model(['purchase_line_id','company_id'])
    env.cr.execute('SELECT id FROM supplier_shortage_claim_line WHERE purchase_line_id IN %s',[ids])
    invalidate_lines(env,[row[0] for row in env.cr.fetchall()],'Material %s source changed; reconcile and reverify affected conclusions.'%kind,kind)


def from_moves(env, ids):
    ids=tuple(set(ids))
    if not ids: return []
    env.cr.execute('''WITH RECURSIVE ancestry(id) AS (
        SELECT id FROM stock_move WHERE id IN %s
        UNION SELECT parent.id FROM ancestry a JOIN LATERAL (
            SELECT move_orig_id AS id FROM stock_move_move_rel WHERE move_dest_id=a.id
            UNION SELECT origin_returned_move_id FROM stock_move WHERE id=a.id AND origin_returned_move_id IS NOT NULL
        ) parent ON TRUE)
        SELECT DISTINCT purchase_line_id FROM stock_move WHERE id IN (SELECT id FROM ancestry) AND purchase_line_id IS NOT NULL''',[ids])
    return [row[0] for row in env.cr.fetchall()]


class SourcePurchaseLine(models.Model):
    _inherit='purchase.order.line'
    def write(self, vals):
        result=super().write(vals)
        if vals.keys() & {'product_qty','product_id','uom_id','price_unit','discount','tax_ids'}:
            invalidate_po_lines(self.env,self.ids,'purchase')
        return result


class SourcePurchase(models.Model):
    _inherit='purchase.order'
    def write(self,vals):
        result=super().write(vals)
        if vals.keys() & {'state','partner_id','company_id','currency_id','document_tax_mode'}:
            invalidate_po_lines(self.env,self.order_line.ids,'purchase')
        return result


class SourceStockMove(models.Model):
    _inherit='stock.move'
    @api.model_create_multi
    def create(self, vals_list):
        records=super().create(vals_list)
        invalidate_po_lines(self.env,from_moves(self.env,records.ids),'receiving')
        return records
    def write(self, vals):
        before=from_moves(self.env,self.ids) if vals.keys() & {'purchase_line_id','origin_returned_move_id','move_orig_ids'} else []
        result=super().write(vals)
        if vals.keys() & {'state','quantity','uom_id','product_uom_qty','purchase_line_id','origin_returned_move_id','move_orig_ids','to_refund','location_id','location_dest_id','date'}:
            invalidate_po_lines(self.env,before+from_moves(self.env,self.ids),'receiving')
        return result


class SourceStockDeletion(models.Model):
    _inherit='stock.move'
    def unlink(self):
        ids=from_moves(self.env,self.ids)
        result=super().unlink()
        invalidate_po_lines(self.env,ids,'receiving')
        return result


class SourceMoveLine(models.Model):
    _inherit='stock.move.line'
    @api.model_create_multi
    def create(self,vals_list):
        records=super().create(vals_list)
        invalidate_po_lines(self.env,from_moves(self.env,records.move_id.ids),'receiving')
        return records
    def unlink(self):
        ids=from_moves(self.env,self.move_id.ids)
        result=super().unlink()
        invalidate_po_lines(self.env,ids,'receiving')
        return result

    def write(self,vals):
        moves=self.move_id.ids
        result=super().write(vals)
        if vals.keys() & {'quantity','uom_id','move_id','location_id','location_dest_id'}:
            invalidate_po_lines(self.env,from_moves(self.env,moves+self.move_id.ids),'receiving')
        return result


class SourcePicking(models.Model):
    _inherit='stock.picking'
    def write(self,vals):
        result=super().write(vals)
        if vals.keys() & {'state','backorder_id','location_id','location_dest_id'}:
            invalidate_po_lines(self.env,from_moves(self.env,self.move_ids.ids),'receiving')
        return result


class SourceAccountLine(models.Model):
    _inherit='account.move.line'
    @api.model_create_multi
    def create(self, vals_list):
        rows=super().create(vals_list)
        invalidate_po_lines(self.env,rows.purchase_line_id.ids,'billing')
        return rows
    def write(self,vals):
        old=self.purchase_line_id.ids
        result=super().write(vals)
        if vals.keys() & {'purchase_line_id','quantity','product_uom_id','product_id','price_unit','discount','tax_ids','move_id'}:
            invalidate_po_lines(self.env,old+self.purchase_line_id.ids,'billing')
        return result
    def unlink(self):
        ids=self.purchase_line_id.ids
        result=super().unlink()
        invalidate_po_lines(self.env,ids,'billing')
        return result


class SourceBill(models.Model):
    _inherit='account.move'
    def write(self,vals):
        result=super().write(vals)
        if vals.keys() & {'state','partner_id','currency_id','move_type','ref','invoice_date','date','reversed_entry_id'}:
            invalidate_po_lines(self.env,self.invoice_line_ids.purchase_line_id.ids,'billing')
        return result


class EvidenceClaimLineChanges(models.Model):
    _inherit='supplier.shortage.claim.line'
    def write(self, vals):
        result=super().write(vals)
        if vals.keys() & {'proposed_qty','claim_uom_id','purchase_line_id','claim_id'}:
            invalidate_lines(self.env,self.ids,'Claim quantity or identity changed; reverify scoped evidence.','quantity')
        return result
    def action_release_allocation(self):
        result=super().action_release_allocation()
        invalidate_lines(self.env,self.ids,'Draft allocation released; reevaluate remaining lines.','quantity')
        return result


def invalidate_policy(env, company_ids, scenario=None):
    if not company_ids or not (env.registry.ready or getattr(env.registry,'_ssc_evidence_schema_ready',False)): return
    condition=' AND evidence_scenario=%s' if scenario else ''
    values=[tuple(company_ids)]+([scenario] if scenario else [])+[env.uid]
    env.cr.execute("""WITH changed AS (UPDATE supplier_shortage_claim SET readiness_dirty=TRUE,
        readiness_status='incomplete',readiness_message='Evidence policy changed; reevaluate current requirements.'
        WHERE company_id IN %s AND state IN ('draft','under_review') AND NOT readiness_dirty"""+condition+""" RETURNING id,company_id)
        INSERT INTO supplier_shortage_claim_audit (claim_id,company_id,event_type,actor_id,event_datetime,change_summary)
        SELECT id,company_id,'readiness_invalidated',%s,statement_timestamp() AT TIME ZONE 'UTC','Evidence requirements policy changed.' FROM changed""",values)
    env['supplier.shortage.claim'].invalidate_model(['readiness_dirty','readiness_status','readiness_message','readiness_checklist'])
    if env.registry.ready or getattr(env.registry,'_ssc_approval_schema_ready',False):
        env.cr.execute('''SELECT l.id FROM supplier_shortage_claim_line l
            JOIN supplier_shortage_claim c ON c.id=l.claim_id
            WHERE c.company_id IN %s'''+condition,[tuple(company_ids)]+([scenario] if scenario else []))
        ids=[r[0] for r in env.cr.fetchall()]
        if ids:
            invalidate_approvals(env,ids,'Evidence requirements policy changed; obtain fresh approval.')


class SourceCompanyPolicy(models.Model):
    _inherit='res.company'
    def write(self,vals):
        result=super().write(vals)
        if vals.keys() & {'ssc_min_verified_evidence'}:
            invalidate_policy(self.env,self.ids)
        if vals.keys() & {'tax_calculation_rounding_method','account_price_include','currency_id'}:
            self.env.cr.execute('SELECT id FROM purchase_order_line WHERE company_id IN %s',[tuple(self.ids)])
            invalidate_po_lines(self.env,[row[0] for row in self.env.cr.fetchall()],'purchase')
        return result


class SourceTaxPolicy(models.Model):
    _inherit='account.tax'
    def write(self,vals):
        result=super().write(vals)
        if vals.keys() & {'amount','amount_type','price_include_override','sequence','include_base_amount','is_base_affected','children_tax_ids'}:
            field=self.env['purchase.order.line']._fields['tax_ids']
            children=self._fields['children_tax_ids']
            # All SQL identifiers are native ORM field metadata, never client input.
            from odoo.tools import SQL
            self.env.cr.execute(SQL("""WITH RECURSIVE taxes(id) AS (SELECT id FROM account_tax WHERE id IN %s
                UNION SELECT rel.%s FROM %s rel JOIN taxes t ON rel.%s=t.id)
                SELECT DISTINCT rel.%s FROM %s rel WHERE rel.%s IN (SELECT id FROM taxes)""",
                tuple(self.ids),SQL.identifier(children.column1),SQL.identifier(children.relation),SQL.identifier(children.column2),
                SQL.identifier(field.column1),SQL.identifier(field.relation),SQL.identifier(field.column2)))
            invalidate_po_lines(self.env,[row[0] for row in self.env.cr.fetchall()],'purchase')
        return result


class SourceCurrencyRounding(models.Model):
    _inherit='res.currency'
    def write(self,vals):
        result=super().write(vals)
        if 'rounding' in vals:
            self.env.cr.execute('SELECT l.id FROM purchase_order_line l JOIN purchase_order p ON p.id=l.order_id WHERE p.currency_id IN %s',[tuple(self.ids)])
            invalidate_po_lines(self.env,[row[0] for row in self.env.cr.fetchall()],'purchase')
        return result


class EvidenceDetectionChanges(models.Model):
    _inherit='supplier.shortage.receipt.exception'

    @api.model
    def _synchronize(self, scope, findings):
        before=self.search(scope).filtered(lambda r:r.reconciliation_kind=='detection')
        basis={r.id:(r.source_signature,r.review_state,r.candidate_qty,r.evidence_complete) for r in before}
        records=super()._synchronize(scope,findings)
        changed=records.filtered(lambda r:r.reconciliation_kind=='detection' and r.id in basis and
            basis[r.id]!=(r.source_signature,r.review_state,r.candidate_qty,r.evidence_complete))
        invalidate_po_lines(self.env,changed.purchase_line_id.ids,'candidate')
        return records

    def _transition(self, target, event):
        before={r.id:r.review_state for r in self if r.reconciliation_kind=='detection'}
        result=super()._transition(target,event)
        changed=self.filtered(lambda r:r.id in before and before[r.id]!=r.review_state)
        invalidate_po_lines(self.env,changed.purchase_line_id.ids,'candidate')
        return result
