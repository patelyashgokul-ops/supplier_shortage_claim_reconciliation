from .preparation_fixtures import submit_prepared, prepare_review, additional_receipt
from copy import deepcopy

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import new_test_user

from .test_receipt_integration import ReceiptIntegrationCase


class VendorBillingCase(ReceiptIntegrationCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accounts = {}
        for company in cls.company | cls.other:
            Account = cls.env['account.account'].with_company(company)
            accounts = {}
            for kind,account_type,code in [('expense','expense','S08E'),('payable','liability_payable','S08P'),('receivable','asset_receivable','S08R'),('income','income','S08I')]:
                accounts[kind] = Account.create({'name':'SSC Billing '+kind,'code':code,'account_type':account_type,
                    'company_ids':[Command.set(company.ids)],'reconcile':kind in ('payable','receivable')})
            accounts['journal'] = cls.env['account.journal'].with_company(company).create({'name':'SSC Vendor Billing Tests','code':'S08V',
                'type':'purchase','company_id':company.id,'default_account_id':accounts['expense'].id})
            cls.supplier.with_company(company).write({'property_account_payable_id':accounts['payable'].id,'property_account_receivable_id':accounts['receivable'].id})
            cls.accounts[company.id] = accounts
        cls.user.write({'group_ids':[Command.link(cls.env.ref('account.group_account_readonly').id)]})
        cls.multi.write({'group_ids':[Command.link(cls.env.ref('account.group_account_readonly').id)]})
        cls.reviewer = new_test_user(cls.env,login='ssc_bill_reviewer',
            groups='supplier_shortage_claim_reconciliation.group_accounts_reviewer,purchase.group_purchase_user,account.group_account_readonly',
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])
        cls.finance = new_test_user(cls.env,login='ssc_bill_finance',
            groups='supplier_shortage_claim_reconciliation.group_finance_manager,purchase.group_purchase_user,account.group_account_readonly',
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])

    def _bill(self, lines, posted=False, move_type='in_invoice', partner=None, currency=None, reference=None, journal=None):
        company = lines[0][0].company_id
        accounts = self.accounts[company.id]
        values = {'move_type':move_type,'company_id':company.id,'journal_id':(journal or accounts['journal']).id,
            'partner_id':(partner or self.supplier).id,'invoice_date':fields.Date.today(),'date':fields.Date.today(),
            'ref':reference,'currency_id':(currency or lines[0][0].currency_id).id,
            'invoice_line_ids':[Command.create({'purchase_line_id':line.id,'product_id':line.product_id.id,
                'quantity':quantity,'product_uom_id':line.uom_id.id,'price_unit':10,'account_id':accounts['income' if move_type.startswith('out_') else 'expense'].id,
                'tax_ids':[Command.clear()]}) for line,quantity in lines]}
        bill = self.env['account.move'].with_company(company).create(values)
        if posted:
            bill.action_post()
            self.assertEqual(bill.state,'posted')
        return bill

    def _billing(self,claim):
        claim.claim_line_ids.invalidate_recordset()
        claim.invalidate_recordset()
        return claim.claim_line_ids

    def _scan(self,claim):
        self._billing(claim)
        claim.action_refresh_billing_review()
        return claim.billing_exception_ids

    def _category(self,claim,category):
        return claim.billing_exception_ids.filtered(lambda row: row.exception_type == category)


class TestVendorBillIntegration(VendorBillingCase):
    def test_multiple_posted_bills_total_200_and_exact_line_source(self):
        order = self._order(200)
        first = self._bill([(order.order_line,120)],True)
        second = self._bill([(order.order_line,80)],True)
        line = self._claim(order).claim_line_ids
        self.assertTrue(line.billing_complete)
        self.assert_qty(line.posted_billed_qty,200)
        self.assert_qty(line.draft_billed_qty,0)
        self.assertEqual(set(line.billing_basis['document_ids']),set((first|second).ids))

    def test_draft_and_posted_are_separate_from_native_qty_invoiced(self):
        order = self._order(200)
        self._bill([(order.order_line,120)],True)
        self._bill([(order.order_line,80)])
        line = self._claim(order).claim_line_ids
        self.assert_qty(order.order_line.qty_invoiced,200)
        self.assert_qty(line.posted_billed_qty,120)
        self.assert_qty(line.draft_billed_qty,80)

    def test_canceled_bill_is_historical_and_not_posted_exposure(self):
        order = self._order(100)
        bill = self._bill([(order.order_line,100)])
        bill.button_cancel()
        claim = self._claim(order)
        self._scan(claim)
        self.assert_qty(claim.claim_line_ids.posted_billed_qty,0)
        self.assert_qty(claim.claim_line_ids.canceled_billed_qty,100)
        self.assertTrue(self._category(claim,'canceled_bill_source_changed'))

    def test_posting_draft_refreshes_source_and_review_snapshot_stays_unchanged(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        bill = self._bill([(order.order_line,100)])
        claim = self._claim(order)
        submit_prepared(claim)
        original = deepcopy(claim.claim_line_ids.valuation_snapshot)
        history = set(claim.audit_event_ids.ids)
        bill.action_post()
        self._scan(claim)
        self.assert_qty(claim.claim_line_ids.posted_billed_qty,200)
        self.assert_qty(claim.claim_line_ids.draft_billed_qty,0)
        self.assert_qty(claim.claim_line_ids.potential_billed_difference_qty,115)
        self.assertTrue(claim.billing_review_warning)
        self.assertTrue(self._category(claim,'bill_source_reconciliation_conflict'))
        self.assertEqual(claim.claim_line_ids.valuation_snapshot,original)
        self.assertTrue(history<=set(claim.audit_event_ids.ids))
        added=claim.audit_event_ids.filtered(lambda event:event.id not in history)
        self.assertEqual(added.mapped('event_type'),['readiness_invalidated'])
        self.assertEqual(claim.state,'under_review')

    def test_single_bill_multiple_pos_and_duplicate_product_lines_do_not_spread_header_total(self):
        first = self._order(100,products=[self.product,self.product])
        second = self._order(200,products=[self.second])
        source = [(first.order_line[0],30),(first.order_line[1],20),(second.order_line,50)]
        bill = self._bill(source,True)
        claim_a,claim_b = self._claim(first),self._claim(second)
        self.assertEqual([row.posted_billed_qty for row in claim_a.claim_line_ids],[30,20])
        self.assert_qty(claim_b.claim_line_ids.posted_billed_qty,50)
        for row in claim_a.claim_line_ids:
            lines = self.env['account.move.line'].browse(row.billing_basis['line_ids'])
            self.assertEqual(lines.purchase_line_id,row.purchase_line_id)
            self.assertNotIn(second.order_line.id,lines.purchase_line_id.ids)
        self.assertEqual(claim_a.billing_document_count,1)
        self.assertEqual(bill.amount_untaxed,1000)

    def test_unmatched_physical_bill_line_is_unknown_and_never_guessed_by_product(self):
        order = self._order(100)
        bill = self._bill([(order.order_line,100)])
        orphan = self.env['account.move.line'].create({'move_id':bill.id,'product_id':self.product.id,'quantity':5,
            'price_unit':20,'account_id':self.accounts[self.company.id]['expense'].id})
        claim = self._claim(order)
        self._scan(claim)
        issue = self._category(claim,'unmatched_vendor_bill_line')
        self.assertEqual(issue.source_bill_line_ids,orphan)
        self.assertFalse(issue.quantity_known)
        self.assertFalse(claim.claim_line_ids.billing_complete)
        self.assertFalse(orphan.purchase_line_id)

    def test_nonphysical_sections_notes_services_and_payment_terms_are_not_goods(self):
        order = self._order(100)
        bill = self._bill([(order.order_line,15)])
        service = self.env['product.product'].create({'name':'SSC Freight Service','type':'service'})
        bill.write({'invoice_line_ids':[Command.create({'display_type':'line_section','name':'Section'}),
            Command.create({'display_type':'line_note','name':'Note'}),
            Command.create({'display_type':'line_subsection','name':'Subsection'}),
            Command.create({'product_id':service.id,'quantity':99,'price_unit':1,'account_id':self.accounts[self.company.id]['expense'].id}),
            Command.create({'product_id':self.product.id,'purchase_line_id':order.order_line.id,'quantity':30,'is_downpayment':True,'price_unit':1,'account_id':self.accounts[self.company.id]['expense'].id}),
            Command.create({'name':'Productless charge','quantity':50,'price_unit':1,'account_id':self.accounts[self.company.id]['expense'].id})]})
        bill.action_post()
        line = self._claim(order).claim_line_ids
        self.assertTrue(line.billing_complete)
        self.assert_qty(line.posted_billed_qty,15)
        self.assertTrue(bill.line_ids.filtered(lambda row: row.display_type=='payment_term'))

    def test_uom_conversion_60_units_are_5_boxes_and_fractional_supported(self):
        units = self.product.uom_id
        box = self.env['uom.uom'].create({'name':'SSC Billing Box 12','relative_uom_id':units.id,'relative_factor':12})
        order = self._order(10,uom=box)
        bill = self._bill([(order.order_line,5)])
        bill.invoice_line_ids.write({'product_uom_id':units.id,'quantity':60})
        bill.action_post()
        line = self._claim(order).claim_line_ids
        self.assert_qty(line.posted_billed_qty,5,box)
        partial = self._bill([(order.order_line,0.25)])
        self._billing(line.claim_id)
        self.assert_qty(line.draft_billed_qty,0.25,box)

    def test_incompatible_bill_uom_requires_review(self):
        order = self._order(10)
        bill = self._bill([(order.order_line,5)])
        bill.invoice_line_ids.product_uom_id = self.env['uom.uom'].create({'name':'SSC unrelated billing dimension'})
        claim = self._claim(order)
        self._scan(claim)
        self.assertFalse(claim.claim_line_ids.billing_complete)
        self.assertTrue(self._category(claim,'incompatible_bill_uom'))

    def test_received_85_billed_85_has_zero_posted_difference_and_draft_is_separate(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._bill([(order.order_line,85)],True)
        self._bill([(order.order_line,15)])
        claim = self._claim(order)
        self._scan(claim)
        self.assertTrue(claim.claim_line_ids.billing_difference_known)
        self.assert_qty(claim.claim_line_ids.potential_billed_difference_qty,0)
        self.assertFalse(self._category(claim,'posted_billed_above_received'))
        self.assert_qty(claim.claim_line_ids.draft_billed_qty,15)

    def test_credit_quantities_remain_separate_and_not_recovery_or_automatic_netting(self):
        order = self._order(100)
        self._validate(self._incoming(order))
        self._bill([(order.order_line,100)],True)
        self._bill([(order.order_line,10)],True,move_type='in_refund')
        self._bill([(order.order_line,5)],move_type='in_refund')
        canceled = self._bill([(order.order_line,3)],move_type='in_refund')
        canceled.button_cancel()
        unrelated = self._bill([(self._order(10).order_line,10)],True,move_type='in_refund')
        claim = self._claim(order)
        self._scan(claim)
        line = claim.claim_line_ids
        self.assert_qty(line.posted_billed_qty,100)
        self.assert_qty(line.posted_credit_qty,10)
        self.assert_qty(line.draft_credit_qty,5)
        self.assert_qty(line.canceled_credit_qty,3)
        self.assertFalse(line.billing_difference_known)
        self.assertNotIn(unrelated.id,line.billing_basis['document_ids'])
        self.assertTrue(self._category(claim,'unexpected_credit_note'))
        self.assertEqual(claim.state,'draft')

    def test_native_reversal_links_are_visible_without_allocation(self):
        order = self._order(10)
        bill = self._bill([(order.order_line,10)],True)
        refund = bill._reverse_moves([{'date':fields.Date.today(),'invoice_date':fields.Date.today()}])
        refund.action_post()
        claim = self._claim(order)
        self.assertIn(refund.id,claim.claim_line_ids.billing_basis['document_ids'])
        self.assert_qty(claim.claim_line_ids.posted_credit_qty,10)
        self.assertFalse(claim.claim_line_ids.billing_difference_known)

    def test_currency_mismatch_preserves_native_amounts_without_conversion(self):
        usd = self.env.ref('base.USD')
        usd.active = True
        currency = usd if usd != self.company.currency_id else self.env.ref('base.INR')
        currency.active = True
        order = self._order(10)
        bill = self._bill([(order.order_line,10)],currency=currency)
        claim = self._claim(order)
        self._scan(claim)
        self.assertTrue(self._category(claim,'currency_mismatch'))
        self.assertFalse(claim.claim_line_ids.billing_complete)
        self.assertEqual(bill.invoice_line_ids.currency_id,currency)
        self.assertEqual(bill.invoice_line_ids.price_subtotal,100)
        self.assertNotEqual(order.currency_id,currency)

    def test_native_tax_inclusive_and_exclusive_amounts_are_preserved(self):
        for included,price in [(True,110),(False,100)]:
            order = self._order(10)
            tax = self.env['account.tax'].create({'name':'SSC Billing Tax '+str(included),'amount':10,'type_tax_use':'purchase',
                'company_id':self.company.id,'price_include_override':'tax_included' if included else 'tax_excluded'})
            bill = self._bill([(order.order_line,1)])
            bill.invoice_line_ids.write({'price_unit':price,'tax_ids':[Command.set(tax.ids)]})
            bill.action_post()
            native = bill.invoice_line_ids
            self.assertEqual(self.company.currency_id.compare_amounts(native.price_subtotal,100),0)
            self.assertEqual(self.company.currency_id.compare_amounts(native.price_total,110),0)
            claim = self._claim(order)
            self.assertIn(native.id,claim.claim_line_ids.billing_basis['line_ids'])
            self.assertEqual(native.price_subtotal,100)

    def test_supplier_commercial_contacts_supported_but_unrelated_supplier_flagged(self):
        order = self._order(10)
        contact = self.env['res.partner'].create({'name':'SSC Supplier Billing Contact','parent_id':self.supplier.id,'type':'invoice'})
        self._bill([(order.order_line,5)],partner=contact)
        claim = self._claim(order)
        self.assertTrue(claim.claim_line_ids.billing_complete)
        other = self.env['res.partner'].create({'name':'Different Legal Supplier'})
        self._bill([(order.order_line,5)],partner=other)
        self._scan(claim)
        self.assertTrue(self._category(claim,'supplier_mismatch'))
        self.assertFalse(claim.claim_line_ids.billing_complete)

    def test_cross_company_bill_link_creation_is_rejected(self):
        order = self._order(10)
        other_wh = self.env['stock.warehouse'].search([('company_id','=',self.other.id)],limit=1)
        other = self._order(10,warehouse=other_wh)
        bill = self._bill([(other.order_line,5)])
        with self.assertRaises(ValidationError),self.env.cr.savepoint():
            bill.invoice_line_ids.purchase_line_id = order.order_line

    def test_company_and_accounting_permissions_block_hidden_exposure(self):
        other_wh = self.env['stock.warehouse'].search([('company_id','=',self.other.id)],limit=1)
        other = self._order(10,warehouse=other_wh)
        bill = self._bill([(other.order_line,10)],True)
        claim = self._claim(other,user=self.multi)
        self.assert_qty(claim.claim_line_ids.posted_billed_qty,10)
        with self.assertRaises(AccessError):
            claim.with_user(self.user).action_view_vendor_bills()
        with self.assertRaises(AccessError):
            claim.with_context(allowed_company_ids=self.company.ids).read(['billing_document_count'])
        local_order = self._order(10)
        local_bill = self._bill([(local_order.order_line,10)],True)
        self.env['ir.access'].create({'name':'SSC denied native vendor document','model_id':self.env['ir.model']._get_id('account.move'),
            'operation':'r','domain':"[] if user.id != %s else [('id','!=',%s)]" % (self.no_stock.id,local_bill.id)})
        local = self._claim(local_order,user=self.no_stock)
        self.assertFalse(local.billing_accessible)
        with self.assertRaises(AccessError):
            local.action_view_vendor_bills()

    def test_hidden_linked_accounting_row_is_unavailable_not_zero(self):
        order = self._order(10)
        bill = self._bill([(order.order_line,10)],True)
        self.env['ir.access'].create({'name':'SSC private bill row','model_id':self.env['ir.model']._get_id('account.move.line'),
            'operation':'r','domain':"[] if user.id != %s else [('id','not in',%s)]" % (self.user.id,bill.invoice_line_ids.ids)})
        claim = self._claim(order)
        self.assertFalse(claim.billing_accessible)
        self.assertFalse(claim.claim_line_ids.billing_complete)
        self.assertFalse(claim.billing_line_preview_ids)
        with self.assertRaises(AccessError):
            claim.action_refresh_billing_review()

    def test_potential_duplicates_require_reference_date_and_full_linked_details(self):
        order = self._order(100)
        self._bill([(order.order_line,50)],True,reference='INV-SSC-08')
        second = self._bill([(order.order_line,50)],reference='INV-SSC-08')
        claim = self._claim(order)
        self._scan(claim)
        self.assertEqual(len(self._category(claim,'potential_duplicate_bill')),1)
        second.ref = 'LEGITIMATE-SECOND-INVOICE'
        self._scan(claim)
        self.assertFalse(self._category(claim,'potential_duplicate_bill').condition_present)
        self.assertEqual(second.state,'draft')

    def test_unchanged_refresh_is_idempotent_and_receiving_refresh_preserves_billing_findings(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        self._bill([(order.order_line,100)],True)
        claim = self._claim(order)
        exceptions = self._scan(claim)
        ids = set(exceptions.ids)
        audit = set(exceptions.audit_event_ids.ids)
        self._scan(claim)
        self.assertEqual(set(claim.billing_exception_ids.ids),ids)
        self.assertEqual(set(exceptions.audit_event_ids.ids),audit)
        claim.action_reconcile_receiving()
        self.assertTrue(all(exceptions.mapped('condition_present')))
        self.assertFalse(claim.receiving_exception_ids & exceptions)

    def test_accounts_review_and_finance_decision_do_not_grant_receiving_edit_authority(self):
        order = self._order(10)
        self._bill([(order.order_line,20)],True)
        claim = self._claim(order)
        reviewer_claim = claim.with_user(self.reviewer)
        self._scan(reviewer_claim)
        issue = self._category(reviewer_claim,'posted_billed_above_received')
        issue.action_start_review()
        issue.write({'review_reason':'Accounts examined billing timing and open delivery commitment.'})
        with self.assertRaises(AccessError):
            issue.action_resolve()
        issue.with_user(self.finance).action_resolve()
        self.assertEqual(issue.resolved_by,self.finance)
        claim.action_reconcile_receiving()
        receiving = claim.receiving_exception_ids[:1].with_user(self.finance)
        with self.assertRaises(AccessError):
            receiving.write({'review_reason':'Finance must not edit receiving decisions.'})
        with self.assertRaises(AccessError):
            issue.write({'affected_quantity':999})

    def test_accounting_permission_loss_hides_saved_billing_history(self):
        order = self._order(10)
        bill = self._bill([(order.order_line,20)],True)
        claim = self._claim(order)
        issue = self._scan(claim).filtered(lambda row: row.reconciliation_kind=='billing')
        self.env['ir.access'].create({'name':'SSC hidden bill source','model_id':self.env['ir.model']._get_id('account.move'),
            'operation':'r','domain':"[] if user.id != %s else [('id','!=',%s)]" % (self.reviewer.id,bill.id)})
        with self.assertRaises(AccessError):
            issue.with_user(self.reviewer).read(['affected_quantity'])
        with self.assertRaises(AccessError):
            issue.audit_event_ids.with_user(self.reviewer).read(['valuation_basis'])

    def test_source_reads_and_refresh_do_not_modify_any_accounting_or_stock(self):
        order = self._order(100)
        self._validate(self._incoming(order),85)
        bill = self._bill([(order.order_line,100)],True)
        claim = self._claim(order)
        moves = order.order_line.move_ids
        original = (bill.read(['state','amount_total','payment_state','write_date']),bill.line_ids.read(['quantity','balance','amount_currency','price_subtotal','write_date']),
                    order.order_line.read(['product_qty','price_unit','write_date']),moves.read(['quantity','state','write_date']))
        balances = self.env['account.partial.reconcile'].search_count([])
        payments = self.env['account.payment'].search_count([])
        entries = self.env['account.move'].search_count([])
        self._scan(claim)
        self._scan(claim)
        claim.action_view_vendor_bills()
        self.assertEqual((bill.read(['state','amount_total','payment_state','write_date']),bill.line_ids.read(['quantity','balance','amount_currency','price_subtotal','write_date']),
                    order.order_line.read(['product_qty','price_unit','write_date']),moves.read(['quantity','state','write_date'])),original)
        self.assertEqual(self.env['account.partial.reconcile'].search_count([]),balances)
        self.assertEqual(self.env['account.payment'].search_count([]),payments)
        self.assertEqual(self.env['account.move'].search_count([]),entries)
        self.assertEqual(claim.state,'draft')

    def test_mixed_bill_states_batch_and_native_navigation_views(self):
        order = self._order(200)
        amounts = {'draft':0,'posted':0,'cancel':0}
        for index in range(12):
            state = ('draft','posted','cancel')[index%3]
            bill = self._bill([(order.order_line,index+1)],posted=state=='posted')
            if state=='cancel':
                bill.button_cancel()
            amounts[state] += index+1
        claim = self._claim(order)
        line = claim.claim_line_ids
        for field,state in [('draft_billed_qty','draft'),('posted_billed_qty','posted'),('canceled_billed_qty','cancel')]:
            self.assert_qty(line[field],amounts[state])
        action = claim.action_view_vendor_bills()
        self.assertEqual(set(self.env['account.move'].with_user(self.user).search(action['domain']).ids),set(line.billing_basis['document_ids']))
        row = claim.billing_line_preview_ids[:1]
        self.assertEqual(row.action_ssc_open_vendor_document()['res_id'],row.move_id.id)
        self.assertIn('Vendor Bill Reconciliation',claim.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_claim_form').id,'form')['arch'])
        for kind in ('list','form'):
            self.assertTrue(row.get_view(self.env.ref('supplier_shortage_claim_reconciliation.view_bill_evidence_'+kind).id,kind)['arch'])
        with Form(claim) as form:
            self.assertEqual(form.purchase_id,order)
        with self.assertRaises(AccessError):
            line.write({'posted_billed_qty':999})
        with self.assertRaises(AccessError):
            claim.write({'review_billing_signature':'forged'})


    def test_matching_foreign_purchase_and_bill_currency_is_preserved(self):
        currency = self.env.ref('base.USD') if self.company.currency_id != self.env.ref('base.USD') else self.env.ref('base.INR')
        currency.active = True
        self.env['res.currency.rate'].create({'currency_id':currency.id,'company_id':self.company.id,'name':fields.Date.today(),'rate':0.25})
        order = self._order(10)
        order.currency_id = currency
        bill = self._bill([(order.order_line,10)],True)
        claim = self._claim(order)
        self.assertTrue(claim.claim_line_ids.billing_complete)
        self.assert_qty(claim.claim_line_ids.posted_billed_qty,10)
        self.assertEqual(claim.currency_id,currency)
        self.assertEqual(bill.invoice_line_ids.currency_id,currency)
        self.assertEqual(currency.compare_amounts(bill.invoice_line_ids.price_subtotal,100),0)
        self.assertNotEqual(currency,self.company.currency_id)

    def test_customer_documents_and_miscellaneous_entries_never_count_as_vendor_billing(self):
        order = self._order(10)
        self._bill([(order.order_line,5)],True)
        accounts = self.accounts[self.company.id]
        sales = self.env['account.journal'].create({'name':'SSC Customer Source Test','code':'S08S','type':'sale','company_id':self.company.id,'default_account_id':accounts['income'].id})
        customer = self._bill([(order.order_line,500)],True,move_type='out_invoice',journal=sales)
        general = self.env['account.journal'].create({'name':'SSC Nonvendor Entry Test','code':'S08G','type':'general','company_id':self.company.id})
        entry = self.env['account.move'].create({'move_type':'entry','journal_id':general.id,'date':fields.Date.today(),
            'line_ids':[Command.create({'account_id':accounts['expense'].id,'debit':1,'purchase_line_id':order.order_line.id,
                        'quantity':1000,'product_id':self.product.id,'product_uom_id':self.product.uom_id.id}),
                        Command.create({'account_id':accounts['payable'].id,'credit':1,'partner_id':self.supplier.id})]})
        entry.action_post()
        claim = self._claim(order)
        self.assert_qty(claim.claim_line_ids.posted_billed_qty,5)
        self.assertNotIn(customer.id,claim.claim_line_ids.billing_basis['document_ids'])
        self.assertNotIn(entry.id,claim.claim_line_ids.billing_basis['document_ids'])


    def test_duplicate_cluster_growth_updates_one_review_instead_of_pairs(self):
        order = self._order(100)
        bills = self._bill([(order.order_line,10)],reference='SSC-DUPLICATE-GROUP')
        bills |= self._bill([(order.order_line,10)],reference='SSC-DUPLICATE-GROUP')
        claim = self._claim(order)
        self._scan(claim)
        original = self._category(claim,'potential_duplicate_bill')
        self.assertEqual(len(original),1)
        for index in range(2):
            bills |= self._bill([(order.order_line,10)],reference='SSC-DUPLICATE-GROUP')
        self._scan(claim)
        self.assertEqual(self._category(claim,'potential_duplicate_bill'),original)
        self.assertEqual(set(original.source_bill_line_ids.move_id.ids),set(bills.ids))
        self.assertEqual(len(original.audit_event_ids.filtered(lambda event:event.event_type=='exception_detected')),1)
        self.assertTrue(all(bill.state=='draft' for bill in bills))


    def test_ordered_and_received_billing_policy_context_updates_without_liability(self):
        order = self._order(10)
        self._bill([(order.order_line,10)],True)
        claim = self._claim(order)
        labels = dict(self.product._fields['purchase_method']._description_selection(self.env))
        for policy in ('purchase','receive'):
            self.product.purchase_method = policy
            self.assertEqual(claim.claim_line_ids.billing_policy,labels[policy])
            self.assert_qty(claim.claim_line_ids.posted_billed_qty,10)
            self.assertEqual(claim.state,'draft')
        self.assertTrue(claim.claim_line_ids.billing_complete)

