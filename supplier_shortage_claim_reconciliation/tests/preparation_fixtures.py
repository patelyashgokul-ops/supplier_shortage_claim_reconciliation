"""Upgrade pre-STEP-10 positive workflow fixtures using real source workflows.

No production validator is patched or bypassed. Negative authorization tests call
public methods directly. Draft-only valuation tests need no source enrollment.
"""
from odoo import Command, fields
from odoo.tests.common import new_test_user


MODULE = 'supplier_shortage_claim_reconciliation.'


def prepare_review(claim):
    claim.ensure_one()
    root = claim.env(user=1)
    company = claim.company_id
    owner = root['res.users'].browse(claim.env.uid)
    owner.write({'group_ids':[Command.link(root.ref('stock.group_stock_user').id),
                             Command.link(root.ref('account.group_account_readonly').id)]})
    login = 'ssc_source_fixture_reviewer_%s'%company.id
    reviewer = root['res.users'].search([('login','=',login)]) or new_test_user(root,login=login,
        groups=MODULE+'group_claim_manager,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
        company_id=company.id,company_ids=[Command.set(company.ids)])
    Claim = root['supplier.shortage.claim'].with_user(reviewer).with_context(allowed_company_ids=company.ids)
    managed = Claim.browse(claim.id)
    managed.write({'shortage_reason':'Verified physical shortage against posted supplier billing',
        'manual_exception_reason':'Pre-existing manual draft now reconciled with reviewed physical and billing evidence',
        'correction_reason':'Authorized correction of the prepared draft quantity'})
    order = root['purchase.order'].browse(claim.purchase_id.id).with_company(company)
    # Close open delivery demand explicitly in these closed-delivery claim fixtures.
    order.picking_ids.filtered(lambda p:p.state not in ('done','cancel')).action_cancel()
    Account = root['account.account'].with_company(company)
    accounts = {}
    for key,kind,code in [('expense','expense','S10E'),('payable','liability_payable','S10P')]:
        accounts[key] = Account.search([('company_ids','in',company.ids),('code','=',code)],limit=1) or Account.create({
            'name':'Claim Preparation Fixture '+key,'code':code,'account_type':kind,
            'company_ids':[Command.set(company.ids)],'reconcile':key=='payable'})
    order.partner_id.with_company(company).property_account_payable_id=accounts['payable']
    Journal = root['account.journal'].with_company(company)
    journal = Journal.search([('company_id','=',company.id),('code','=','S10F')],limit=1) or Journal.create({
        'name':'Claim Preparation Fixture Bills','code':'S10F','type':'purchase','company_id':company.id,
        'default_account_id':accounts['expense'].id})
    positions = order.order_line._ssc_billing_analysis()
    bill_lines=[]
    for line in managed._review_lines().purchase_line_id:
        billed=positions[line.id]['bill_posted']
        if line.uom_id.compare(billed,line.product_qty)<0:
            bill_lines.append(Command.create({'purchase_line_id':line.id,'product_id':line.product_id.id,
                'quantity':line.product_qty-billed,'product_uom_id':line.uom_id.id,
                'price_unit':max(abs(line.price_unit),1),'discount':line.discount,
                'tax_ids':[Command.clear()],'account_id':accounts['expense'].id}))
    if bill_lines:
        bill=root['account.move'].with_company(company).create({'move_type':'in_invoice','company_id':company.id,
            'partner_id':order.partner_id.id,'currency_id':order.currency_id.id,'journal_id':journal.id,
            'invoice_date':fields.Date.today(),'date':fields.Date.today(),'invoice_line_ids':bill_lines})
        bill.action_post()
        assert bill.state=='posted'
    source_lines = managed._review_lines().purchase_line_id
    source_lines.with_user(reviewer).with_context(allowed_company_ids=company.ids).action_run_shortage_detection()
    candidates=root['supplier.shortage.receipt.exception'].with_user(reviewer).with_context(allowed_company_ids=company.ids).search([
        ('reconciliation_kind','=','detection'),('purchase_line_id','in',source_lines.ids)])
    for candidate in candidates:
        if candidate.review_state!='resolved':
            candidate.write({'review_reason':'Receiving position and canceled delivery reviewed; no missing GRN is invented. Purchase and posted bill provenance verified.'})
            candidate.action_verify_candidate()
    for line in managed._review_lines():
        if not line.detection_id:
            candidate = candidates.filtered(lambda rec:rec.purchase_line_id==line.purchase_line_id)
            line.action_bind_reviewed_candidate(candidate.id)
    managed.action_revalidate_preparation()
    evidence=managed.evidence_ids.filtered(lambda e:e.verification_state=='verified')
    if not evidence:
        evidence=root['supplier.shortage.claim.evidence'].with_user(reviewer).with_context(allowed_company_ids=company.ids).create({
            'claim_id':claim.id,'evidence_type':'purchase','description':'Verified order supporting documented warehouse discrepancy',
            'source_purchase_id':order.id,'verification_note':'Confirmed source relevance, receiving provenance and posted invoice quantities'})
        verify_document(evidence)
    complete_evidence(claim)
    claim.invalidate_recordset()
    return claim


def submit_prepared(claim):
    for record in claim:
        prepare_review(record)
    return claim.action_submit_for_review()


def additional_receipt(order, quantity=None):
    """New native receipt after a closed delivery, for source-change regressions."""
    env=order.env(user=1)
    warehouse=order.picking_type_id.warehouse_id
    picking=env['stock.picking'].with_company(order.company_id).create({'partner_id':order.partner_id.id,
        'picking_type_id':warehouse.in_type_id.id,'location_id':env.ref('stock.stock_location_suppliers').id,
        'location_dest_id':warehouse.lot_stock_id.id,'move_ids':[Command.create({'product_id':line.product_id.id,
            'product_uom_qty':quantity if quantity is not None else line.product_qty-line.qty_received,
            'uom_id':line.uom_id.id,'purchase_line_id':line.id,
            'location_id':env.ref('stock.stock_location_suppliers').id,'location_dest_id':warehouse.lot_stock_id.id}) for line in order.order_line if not line.display_type]})
    picking.action_confirm()
    picking.action_assign()
    for move in picking.move_ids:
        move.quantity=move.product_uom_qty
        move.picked=True
    picking.with_context(skip_backorder=True).button_validate()
    assert picking.state=='done'
    return picking

# STEP 11 fixtures use an actual independent business checker. No validators are patched.
def independent_checker(env, company):
    root=env(user=1)
    login='ssc_independent_evidence_%s'%company.id
    checker=root['res.users'].search([('login','=',login)])
    if not checker:
        checker=new_test_user(root,login=login,groups=MODULE+'group_claim_manager,'+MODULE+'group_warehouse_reviewer,'+
            MODULE+'group_accounts_reviewer,purchase.group_purchase_user,stock.group_stock_manager,account.group_account_readonly',
            company_id=company.id,company_ids=[Command.set(company.ids)])
    return checker


def verify_document(evidence):
    for rec in evidence:
        checker=independent_checker(rec.env,rec.company_id)
        rec.with_user(checker).with_context(allowed_company_ids=rec.company_id.ids).action_verify()
    return True


def reject_document(evidence):
    for rec in evidence:
        checker=independent_checker(rec.env,rec.company_id)
        rec.with_user(checker).with_context(allowed_company_ids=rec.company_id.ids).action_reject()
    return True


def complete_evidence(claim):
    root=claim.env(user=1)
    checker=independent_checker(claim.env,claim.company_id)
    maker=claim.env['supplier.shortage.claim.evidence']
    lines=claim._review_lines()
    # Native bill reference covers exact PO-line scope, never an unrelated product.
    for bill in lines.detection_id.source_bill_line_ids.move_id.filtered(lambda b:b.state=='posted' and b.move_type=='in_invoice'):
        covered=lines.filtered(lambda line:line.purchase_line_id in bill.invoice_line_ids.purchase_line_id)
        document=claim.evidence_ids.filtered(lambda e:e.is_current and e.source_bill_id==bill)
        if not document:
            document=maker.create({'claim_id':claim.id,'evidence_type':'bill','description':'Attributable posted supplier billing',
                'source_bill_id':bill.id,'scope_line_ids':[Command.set(covered.ids)],
                'verification_note':'Posted supplier bill and exact purchase line quantities reviewed'})
        if any(e.effective_state!='verified' for e in document): verify_document(document)
    for line in lines:
        observation=root['supplier.shortage.warehouse.observation'].search([('claim_line_id','=',line.id),('is_current','=',True)])
        values={'claim_line_id':line.id,'warehouse_id':claim.purchase_id.picking_type_id.warehouse_id.id,
            'receipt_id':line.detection_id.source_move_ids.picking_id.filtered(lambda p:p.state=='done')[:1].id,
            'observation_type':'delivery_count','observed_qty':line.detection_id.net_received_qty,
            'uom_id':line.purchase_line_id.uom_id.id,'count_method':'documents',
            'explanation':'Documented delivery-specific receiving reconciliation against native receipt provenance; absent receipts are not fabricated.'}
        if not observation:
            observation=claim.env['supplier.shortage.warehouse.observation'].create(values)
        elif not observation.effective_verified:
            action=observation.with_user(claim.env.user).action_replace({'observed_qty':values['observed_qty'],
                'explanation':values['explanation']},'Renewed reconciliation after source or quantity correction')
            observation=claim.env['supplier.shortage.warehouse.observation'].browse(action['res_id'])
        if not observation.effective_verified:
            reviewed=observation.with_user(checker).with_context(allowed_company_ids=claim.company_id.ids)
            reviewed.write({'verification_note':'Independently checked delivery provenance and physical-record consistency'})
            reviewed.action_verify()
    for evidence in claim.evidence_ids.filtered(lambda e:e.is_current and e.verification_state=='verified'):
        if evidence.effective_state!='verified': verify_document(evidence)
    return claim


def submit_evidence_ready(claims):
    for claim in claims: complete_evidence(claim)
    return claims.action_submit_for_review()
