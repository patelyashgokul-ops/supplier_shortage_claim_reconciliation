# Supplier Shortage Claim Reconciliation

Steps 01-08, Odoo 20 Community only. Commercial application; OPL-1.
Publisher metadata is unknown; Odoo's missing-author warning is expected.

## Business and financial foundation

Claims own draft claim lines with supplier/PO references, responsible user, date,
notes and standard Odoo metadata. PO supplier and company must match. Lines must
reference that PO's product lines, with matching products and valid shared or
company-specific relationships. Drafts can be incomplete; submission requires a
valid PO, date and at least one valid, positive line.

Proposed quantity is manual and provisional, never a verified shortage. Claim UoM
is preserved on new linked lines; purchasing UoM comes from Odoo 20's `uom_id`.
Conversion uses a common configured reference tree and native unrounded conversion,
not packaging names or invented ratios. Configured 12-unit boxes convert 6 Units
to 0.5 Box. Product Unit precision controls positive/effectively-zero comparisons.
Inputs beyond its configured digits, nonfinite and negative quantities fail. The
installed UoM has global decimal precision rather than per-UoM rounding; fractional
products need suitable precision. Product-specific whole-unit policy beyond native
configuration is deferred. Multiple proposals for one PO line remain allowed;
future allocations must prevent overlap after verification.

Native purchase/account.tax base-line and rounding helpers compute the provisional
UNTAXED amount, respecting source unit price, discount, tax overrides, document tax
mode and quantity. Precise unit prices are retained until amount rounding. The
amount uses PO transaction currency, never an arbitrary company-currency equivalent.
There is no cross-currency monetary sum. `valuation_status = estimated` means an
estimate exists, not approved entitlement or recovery; `needs_review` numeric
placeholders are not valid estimates and are hidden. API consumers must check it.

Draft estimates follow current PO data. Submission preserves original and normalized
quantities, UoM references/factors, price, discount, tax basis, transaction currency,
rounding and timestamp. Under Review reads the preserved snapshot even if external
PO or UoM configuration changes. Returning to draft permits correction and an
explicit draft refresh; the previous submission remains in immutable audit history.
Submitted currency/UoM identities have restrictive foreign keys. Claim operations
do not write through to purchase, stock or accounting source documents.

## Step 05 native Purchase Order integration

A claim links one actual `purchase.order`; each of its lines references a specific
`purchase.order.line`. Multiple products and repeated products on different PO
lines retain distinct record identities. No supplier/product-name matching or
global unique purchase-line restriction is introduced. Supplier policy is exact
`partner_id` identity, including contacts: similar names and a shared commercial
parent do not automatically establish the same legal supplier.

Initial PO defaults supply its supplier/company and computed transaction currency.
Existing entered suppliers/owners are not overwritten when selecting another order;
inconsistent choices must be corrected explicitly. Company remains immutable after
creation. The claim owner defaults to the acting employee; the PO buyer/date/status
are separate read-only current context. Purchase quantities use `product_qty` in
`uom_id`, not `product_uom_qty` in the product's unit. Existing proposed quantities,
normalization and provisional untaxed pricing remain unchanged.

Only goods (`product.type = consu`) product lines with a valid UoM are eligible for
new physical proposals. Sections, subsections, notes, productless, service, combo
and down-payment lines are excluded by both selection domains and ORM checks.
Unlinked/incomplete drafts remain possible but cannot enter review. Historical
unsupported records are not deleted or silently rewritten.

RFQ (`draft`), RFQ Sent (`sent`), approval-pending (`to approve`) and cancelled
(`cancel`) sources may be referenced during draft preparation, but cannot support
new review submission. Only confirmed (`purchase`) orders qualify. There is no
cancellation exception policy, no legacy `done` state assumption, and no automatic
PO confirmation/cancellation, receipt validation or vendor communication.

The native PO form has Create Shortage Claim and a Shortage Claims Smart Button.
Creation opens an unsaved form with validated PO/supplier/company/employee defaults,
no generated claim lines and no saved record or consumed claim reference until save.
Cancelling the form leaves no empty claim. Actions ignore forged active/default IDs
and validate the actual selected PO. The count uses one access-aware grouped query,
with no elevated business reads. Zero claims opens a clear empty list. Counts and
actions respect activated companies and claim-role access; no claim access yields
zero count rather than disclosure. The existing claim form opens the exact PO, and
the line form opens its exact native source line. Native purchase views are inherited,
not replaced. Claims can be searched by actual products and grouped by PO without
duplicate parent results.

Claim read permission does not imply native PO read permission. User-facing source
actions check both. Users without source rights see Source Unavailable, blank
current PO context, and unavailable draft valuation; historical claim snapshots
remain governed by claim access. No sudo is used to make a purchase action work.

New review submissions preserve PO identity/supplier/company/currency/state alongside
the existing pricing/UoM snapshots. Current-source checks flag material differences
in state, supplier, company, currency, selected line, product, quantities, UoMs,
prices, discounts or taxes. Submitted values and audit remain unchanged. Historical
reviews lacking this new check show an explicit warning, not invented history.
Returning to draft allows controlled relationship correction/refresh and a fresh
review. Source comparison is current-versus-submitted, not a recorder of every
intermediate edit made directly in native purchase applications. Source writes are
not globally overridden. Purchase and source-line references remain restrictive
against deletion. Read-only context checks and navigation produce no audit noise;
initial links, changed links, corrections and review submission use formal history.

Read-only physical receipt integration is provided in Step 06 below; final shortage verification remains deferred.
Step 08 below provides read-only vendor billing sources; final financial eligibility remains deferred.
Confirmed recoverable shortage calculation has not yet been implemented.

## Effective roles

| Role | Claim/line access | Current workflow authority |
| --- | --- | --- |
| Warehouse User | Read authorized company records | None |
| Purchase Claim User | Create/read/update drafts | Submit valid drafts for review |
| Purchase Claim Manager | Inherits Purchase Claim User | Return review claims for correction |
| Accounts Reviewer | Read authorized company records | None |
| Finance Manager | Inherits Accounts Reviewer | None; future financial approval is separate |
| Module Administrator | Read authorized company records | None solely from configuration role |

Module Administrator no longer implies Purchase Claim Manager. Assign a business
role separately where needed. Native Odoo 20 explicit `group_ids` and computed
`all_group_ids` preserve separately assigned roles when implications change.
Cumulative read/configuration roles do not grant edit or review authority. Ordinary
internal, portal and public users have no claim/line/audit access. Claim roles do
not implicitly grant native purchase/accounting/stock permissions; assign those
separately when needed. Warehouse access currently means explicitly authorized
company scope, not a speculative assignment/warehouse model.

## Company, workflow and field protection

Odoo 20 `ir.access` uses model names and CRUD operation strings. Group grants combine
by union; group-less company records are global restrictions combined by intersection.
Claims, lines and audit events are restricted to activated permitted companies.
Audit access additionally requires access to the parent through the native `access`
domain operator. Searches, direct reads and explicit exports are tested; export
methods explicitly check read access even when only record IDs are requested.
Shared suppliers/products remain valid. A claim's company is immutable after
creation so its history cannot be moved into another company's visibility.

Only Draft -> Under Review and manager-authorized Under Review -> Draft exist.
New review submissions require a confirmed native PO (`state = purchase`).
State, review metadata, standard metadata, references, audit relationships,
normalized quantities, computed pricing/status and snapshots reject generic writes.
Context flags grant no authority; forged context defaults cannot inject audit
records, actors, metadata or valuations. Parent and direct child create/write,
reparent and One2many paths enforce review protection. Managers return records to
draft before corrections. Copying produces a new draft reference, no review
metadata/snapshot, and fresh history rather than copied audit events.

Transitions validate complete recordsets before mutation and run inside savepoints.
Rejected or interrupted operations roll back snapshots, state and formal events,
even if an ORM caller catches the exception. Child edits lock and version their
parent row so concurrent review must retry against fresh lines. Native row locks
reject conflicting operations; they do not silently approve or discard edits.

Deletion remains denied for every business role, including drafts. Claim deletion
also has a server-side guard; audit mutation/deletion is always rejected by its
public ORM methods. Historical audit parent references use `ondelete=restrict`.
No archive feature is provided, so archiving is not an alternate access path.
Database administration and deliberate uninstall are outside normal business access;
this is not cryptographic protection against a database administrator.

## Formal audit history

`supplier.shortage.claim.audit` is the authoritative business history. It records:
claim creation, added lines, material header/relationship/quantity changes, review
submission, authorized return/correction and explicit draft valuation refresh.
Submission events retain immutable valuation basis and readable details; refreshes
retain their currency/UoM context. Identical display names do not hide identity
changes. Reads, onchanges, notes and genuine no-op field writes are not logged.

Events record server actor/time, fixed claim/company, old/new state, summary and
reason where appropriate. Normal users, managers and configuration administrators
cannot fabricate, alter, copy or delete events. One narrowly scoped private append
path elevates only insertion after checking the caller's write access to the exact
claims; it derives actor/company/time itself, returns no elevated recordset, and
performs no elevated business reads. No context token authorizes this path.
There are no independent commits or background audit queues in module methods.
Direct native PO/tax/UoM edits are outside claim event hooks; the next explicit
refresh or submission captures their current basis, while submitted versions stay
frozen. Audit storage grows with line counts and review cycles; larger-volume
benchmarking and retention/reporting policy remain integration/release work.

Audit History is a native read-only form tab with date, event, actor, states,
description, reason and readable valuation details. New formal events are not
mirrored into duplicate custom chatter messages. Native field tracking and internal
comments remain supplementary, and old Step 01-03 chatter remains untouched.
Formal history is the consistent source of truth going forward. Manager return
currently records the standard correction reason; richer approval/reason workflows
remain future work.

## Attachments

Claim-linked documents use native parent access plus a global attachment read
restriction. Creation, relinking and mutation check their actual claim and company;
review documents cannot be silently changed. New public/token sharing is denied.
Content/stream checks retain the real request user's access even when native token
serving arrives elevated, preventing legacy public/token routes from bypassing
claim visibility. Formal audit events accept no editable attachments. Unrelated
Odoo attachments retain their native behavior; no customer attachments are deleted
or rewritten by this upgrade. Full evidence management remains a later feature.

## References and upgrade policy

SSC references are globally unique, immutable and backed by a SQL unique constraint
plus standard Odoo sequences. Sequence selection uses each claim's target company
and explicit sequence date. Company-specific sequences need distinct prefixes;
misconfigured collisions fail safely. Gaps are normal. Copy/batch creation and real
concurrent creation are tested; upgrades never reset the sequence.

Version `20.0.5.0.0` preserves technical names/XML IDs and prior quantities, records,
references and financial snapshots. Step 02 date backfill uses existing creation
metadata. Legacy unset quantities are never invented. Earlier review claims without
a captured valuation remain Needs Valuation Review until controlled correction.

The Step 04 post-migration adds one clearly labeled Audit Logging Enabled event
per existing claim, retaining any existing submitted basis. It does not fabricate
past creation/review events or reconstruct unknown actors/dates. Historical chatter,
attachments and snapshots remain intact. New audit events start when logging was
enabled. The role implication change removes implicit administrator business rights;
separately assigned business roles remain explicit and intentional.

## Reusable local testing and foundation gate

Verified source: `C:/PycharmProjects/odoo20/odoo-20.0`, release 20.0; Python 3.14.6
at `C:/PycharmProjects/odoo20/.venv/Scripts/python.exe`; PostgreSQL 18.4. No core
or dependency files were changed. This workspace has no Git repository.
`purchase_stock` supplies the required Community purchase, inventory, accounting,
product/UoM and mail dependencies.

Development tooling is in ignored `.testing/` and excluded from customer delivery.
The existing runner accepts only two fixed disposable database names, reads existing
local credentials, disables HTTP/cron and keeps detailed logs local. It rejects
zero tests, incomplete shutdown, errors and initialization failures. The fresh
profile refuses an already existing database; it never drops a database. Unrelated
productivity apps are disabled for test initialization. Never target production.

```powershell
$python = 'C:/PycharmProjects/odoo20/.venv/Scripts/python.exe'
# Level 1: Purchase Order integration
& $python .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestPurchaseIntegration
# Level 2 / upgrade side of the foundation gate: all APP 05 tests
& $python .testing/run.py --mode update --level regression
# Level 3: initial installation and full suite on the unused fresh profile
& $python .testing/run.py --mode install --level foundation --database ssc_claim_test_20_step04_fresh_20261102
# Subsequent quality checks on that same fresh-test environment use update
& $python .testing/run.py --mode update --level foundation --database ssc_claim_test_20_step04_fresh_20261102
# Persisted Step 01-03 compatibility checks, plus separate real transactions
& $python .testing/check_step05_upgrade.py
& $python .testing/check_step04_concurrency.py
```

ORM tests use TransactionCase and real non-superuser accounts. The separate
concurrency script uses committed fixtures and four simultaneous transactions on
the dedicated upgrade database; it checks reference uniqueness, edit/review
contention, absence of a false success event, and fresh retry valuation. Committed
fixture data stays in that test database only. Read-only verification scripts use
explicit rollback because Odoo cursor context exit commits on success. An older
local checker committed a correction in the disposable legacy fixture; Step 04
compares that fixture to its immutable upgrade baseline instead of resetting it.
Upgrade checks compare saved records
and snapshots captured before this upgrade, rather than relying on fresh install
success. No general Odoo global suite is run.

Earlier evidence: Step 01 installed and passed 8 module tests; Step 02 passed 30;
Step 03 passed 52. Step 04 executed evidence (2026-11-02): targeted security/audit 20/20; existing-DB
regression 72/72; fresh installation 73/73; follow-up foundation update on the fresh
database 72/72. All final runner executions exited 0 with zero failures/errors.
Persisted Step 01-03 references/lines/snapshots and one-time upgrade baselines passed
verification. Four real concurrent creations produced distinct references; edit vs
review contention rejected the conflicting submission without a success event,
and a fresh retry captured the edited quantity. The foundation quality gate passed.
Results are retained in `.testing/step04-results.json`. Detailed logs stay in
`.testing/logs/`. This gate is the APP 05 foundation gate, not a future full release
certification or production migration guarantee.

Step 05 upgrade verification uses `.testing/check_step05_upgrade.py`; its baseline
was captured before module changes. The check compares original references, lines,
valuation snapshots and immutable audit data without committing changes. Step 05
execution evidence (2026-11-02): all 21 purchase integration tests passed; the
focused repair/UI rerun passed 3/3; final module regression passed 93/93, including
all 72 earlier tests adapted only for the explicit confirmed-PO policy and initial
PO defaults. Final failures/errors: zero. Module update and exact comparison of
pre-upgrade references, lines, valuation snapshots and audit history passed.
Evidence is retained in `.testing/step05-results.json`. No new concurrency-testing
claim is made for Step 05; Step 04's separate evidence remains historical.

## Future boundaries

Verified shortage, financially claimable
quantity, replacement/credit/refund allocations, reversals, recovery reports,
dashboards and warehouse-specific assignment remain future work. Proposed quantity remains the only implemented claim-demand quantity; receiving positions are read-only source evidence. Pending delivery is not a confirmed
shortage; supplier acceptance is not verified recovery. Credits and their payments
must not be counted twice, physical and monetary recovery stay distinct, and claim
closure does not imply full recovery. Future integrations must preserve the same
company/currency integrity and immutable audit boundary.


## Step 06 - Goods receipt evidence

`purchase_stock` provides the authoritative PO-line -> `stock.move.purchase_line_id`
relationship. Live, non-stored summaries follow actual move origins, destinations
and return provenance. No receipt-link table or second stock ledger is created.
Completed supplier boundaries use **done move.quantity**, normalized through native
UoM conversion without intermediate rounding. Planned/reserved quantities on
unfinished moves never count as received. Move lines remain available for native
lot/serial traceability and are never added again to move totals.

Supplier -> internal movements with verified PO provenance contribute gross
receipts. Internal/input/quality/transit transfers are supporting evidence only:
one-, two- and three-step receiving count the supplier boundary once. Receipt-like
inter-warehouse transfers, customer deliveries, inventory adjustments and scrap
do not establish supplier delivery. Ambiguous origins, company/warehouse mismatch,
unsupported UoM or product configuration require review rather than guessed totals.
Goods, including untracked goods, need actual validated stock moves and the native
stock-moves receipt method; services/manual receiving methods are unsupported.

Linked completed internal -> supplier returns require native returned-move ancestry
to a completed supplier receipt. Partial/multiple returns and subsequent replacement
receipts remain distinct movements. Gross receipts, linked returns and net position
are separate; native PO `qty_received` is not subtracted again. **Not Yet Received**
is ordered minus gross supplier deliveries, floored at zero with native UoM precision;
returns do not retrospectively prove an original delivery shortage. Net retained
quantity is displayed separately. Pending supplier demand and its backorder subset
are informational, not an additional quantity to add to received totals. Canceled
backorders are disclosed and never establish supplier liability. Over-receipts and
return activity have explicit independent receiving statuses.

The Goods Receipt Reconciliation page shows positions per exact PO line and a
40-movement preview. Navigation opens paginated native receipt documents or a
read-only movement evidence view, including locations, warehouse where known,
backorder, return and lot/serial details. Duplicate claim lines sharing one PO line
do not multiply header receipt counts. Attachments are neither copied nor exposed
through elevated access; native Inventory document permissions apply.

All reads use the acting user's company and source-document permissions. Narrow
server-built internal ORM domains test whether already-authorized purchase lines
have inaccessible movement provenance; no hidden identities, amounts or counts are
returned. Missing stock, warehouse or movement-detail access makes the position
**unavailable**, not zero. Numeric fields are unusable placeholders unless
`receiving_complete` is true; the UI hides those amounts. A custom consumer must
check that flag. Warehouse assignment beyond native Inventory access rules is not
introduced. Review submission records only a source fingerprint, not frozen stock
quantities. Later receipts/backorder changes/returns refresh live evidence and warn
on review claims without altering submitted financial snapshots or audit events.
Older review claims have no receiving baseline and are explicitly flagged.

Inventory operations are never written by this integration. Preliminary internal
review may proceed with incomplete receiving evidence, which remains flagged;
no final approval operation exists. Step 06 deferred vendor billing; Step 08 below provides read-only vendor billing integration. Confirmed financial shortage detection is NOT complete. Supplier claim
approval and settlement are NOT implemented by this step.

Reuse the existing disposable database runner (paths configured in `.testing`):

```powershell
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestReceiptIntegration
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step06_upgrade.py
```

Full logs stay in `.testing/logs`; zero discovery or incomplete execution fails.
The Step 06 upgrade checker compares a pre-change disposable baseline; this is
not certification of an unknown production database migration. No additional
concurrency claim is made in this step.


Step 06 executed evidence (2026-11-02): receipt-class targeted run **28/28**,
follow-up source-ownership/no-PO-access targeted run **2/2**, final module regression
**122/122** (29 receiving tests plus 93 earlier tests), zero failures/errors.
Real native receipt validation, partial backorders, two-/three-step routes, supplier
returns and return-of-return replacements were exercised. Module update and exact
pre-change reference/line/valuation/audit comparison passed; historical reviews show
the new baseline warning without rewritten history. Evidence is retained in
`.testing/step06-results.json`. No production database was used.


## Step 07 - Receiving integrity and exception review

The Step 06 physical movement engine remains authoritative for live receiving
summaries. A targeted **Refresh Receiving Review** action analyzes the selected
claim lines in batches. No cron, global installation scan, stock correction, PO
change or financial transaction is introduced. Warehouse corrections are performed
only through standard Inventory operations; a later refresh detects their effects.

Quantity terms are deliberately distinct:

| Measure | Meaning in purchase UoM |
| --- | --- |
| Gross supplier received | Qualifying completed supplier-boundary receipts |
| Supplier returned | Completed, provenance-linked supplier returns |
| Net supplier received | Gross minus linked returns, once |
| Pending delivery | Remaining active inbound demand; completed/canceled parents excluded |
| Pending backorders | The backorder subset of pending delivery; never added again |
| Canceled supplier demand | Historical demand on attributable canceled inbound moves |
| Unreceived difference | max(ordered minus net received, zero) |
| Over-received difference | max(net received minus ordered, zero) |

Step 06's compatible `receipt_unreceived_qty` retains its explicitly **gross-basis**
meaning. Step 07 uses `receipt_delivery_difference_qty` for the separate net-basis
comparison. A return therefore affects the latter without being mislabeled an
original delivery shortage. Pending, canceled demand and delivery differences may
overlap; they are never summed into a claim quantity or financial amount. Native
Product Unit precision governs comparisons and native UoM conversion is retained.
No universal over-delivery tolerance is assumed.

Successive backorders retain native picking lineage. Only active move demand counts
as pending; a completed 60-unit parent subsequently split into 30 done and 30 open
does not contribute another 60. Overlapping active ancestors, wrong purchase-line
lineage or inaccessible provenance invalidate the receiving basis for review.
Canceled operations remain review evidence even if a later receipt fulfills the
order: their demand is historical, not an assertion of current missing quantity or
supplier liability. Replaced/canceled arrangements require human verification.

Native PO `qty_received` is checked against its installed Odoo 20 movement policy,
including return/refund flags, dropship exclusions and native conversion rounding.
The comparison deliberately avoids calling the native helper that can write
tracking messages. A difference between gross and net, or an explained native-policy
difference from physical net, is not a conflict. A native value inconsistent with
its own eligible movements creates a high-priority source conflict. Neither source
is overwritten. Incomplete/ambiguous evidence has an **unknown** affected quantity,
not a fabricated zero estimate. The UI hides those numeric placeholders.

### Persistent exceptions and source ownership

`supplier.shortage.receipt.exception` stores review findings, not a stock ledger.
Categories cover pending delivery, canceled receiving/backorders, over-receipt,
native source conflicts, missing purchase links, incompatible UoM, unexpected
returns, ambiguous movements, inconsistent backorders and source changes. References
use a separate standard `RRE/` sequence; claim references and sequences are preserved.
Each finding references its exact PO/line and authorized movement IDs. Unlinked
supplier moves are detectable only as siblings within an actual native PO picking;
no supplier/product-name/text matching or global orphan allocation is attempted.
An isolated move with no reliable native PO relationship remains outside this scan.

A stable database-unique source key combines company, PO, exact line, category and
operation/event identity. One unresolved logical condition has one record across
claims and repeated scans. Line-level balance conditions share a record; distinct
canceled operations remain distinct. A claim-specific submitted-source warning has
its own claim event key. Source snapshots/fingerprints contain no monetary recovery.

Refresh records a claim-owned fingerprint baseline and timestamp. Unchanged
exceptions retain their detection timestamp, state, decision and audit history;
only the explicit refresh timestamp advances. Changed conditions update their
source evidence and append history. Conditions no longer found are marked absent,
not silently deleted or financially resolved. A recurring materially changed
condition reopens its existing resolved/dismissed record and preserves the prior
actor, date and decision reason in immutable history. Source-change findings remain
present until reviewed; unchanged refreshes do not repeatedly generate events.
Old claims have no invented pre-Step-07 refresh baseline. Existing Step 06 review
fingerprints still warn when submitted receiving evidence changes.

### Review roles, history and UI

New findings are assigned to the authorized refresh operator. Purchase claim
managers may reassign to active warehouse/purchase reviewers authorized for the
company. Assigned warehouse/purchase users can start review and record notes.
Only purchase claim managers can resolve, dismiss or manually reopen. Decisions
require a meaningful reason; manual reopening requires a new explanation. Managers
may record that explanation on a completed review, while its previous decision
reason and audit events remain intact. Accounts/finance/configuration roles have
read access subject to source permissions, without acquiring decision authority.
Combined groups do not replace the explicit manager check.

The native workbench provides exception and backorder lists, filters, grouping,
assignment, source navigation and immutable history. The claim's Receiving Integrity
Summary presents net delivery differences and open findings alongside the existing
read-only receipt evidence. Source changes never alter proposed quantities,
submitted financial snapshots or claim lifecycle state. No claim approval action is
added. Resolving an exception completes a human review; it does not assert that
Inventory was corrected or that a supplier owes money.

The existing formal audit model now supports exactly one claim **or** receiving
exception parent, enforced in the database. Existing claim events are preserved.
Detection, assignment, notes, decisions, reopening, disappearance and material
source changes use the same trusted actor/time and private append-only insertion
boundary. Ordinary ORM create/write/unlink cannot forge or remove audit events.
Failures and rollbacks remove associated success events. Exception deletion/copying
is prohibited. No attachment upload/approval workflow is introduced for exceptions;
original Inventory documents retain native attachment access. Audit source IDs and
UoM context remain traceable even if standard Inventory later removes an otherwise
removable draft/canceled source; APP 05 never deletes those documents itself.

Company and native purchase/stock permissions apply to records, search, counts,
grouping, review actions and navigation. Stored review quantities and their history
are hidden when their supporting moves, pickings, move-line detail, locations or
warehouse are inaccessible. An incomplete refresh fails atomically and cannot
replace prior findings with zero quantities. Shared goods/suppliers remain valid.
Source PO locks plus the unique source key serialize explicit scans; concurrent
requests may require a fresh-transaction retry. Model methods never commit.

### Step 07 verification

Reuse the existing dedicated disposable database runner:

```powershell
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestReceivingExceptions
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step07_upgrade.py
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step07_concurrency.py
```

The separate concurrency check uses real independent transactions and bounded
fresh-transaction retries on the disposable database. It deliberately retains its
small immutable review fixture for inspection. Full logs remain local. The upgrade
checker compares saved pre-Step-07 claim references, lines, valuations and audit
history; it is not certification of an unknown production migration.

Vendor billing was deferred at Step 07; Step 08 below now provides its read-only source integration. Confirmed recoverable shortage
calculation is not yet implemented. Supplier claim financial settlement is not
implemented in Step 07. Detection is an explicit current-source observation, not a
complete recorder of every intervening Inventory edit; only observed material
changes are audited. Warehouse access follows native rules, without inventing new
warehouse ownership or assignment policies.


Step 07 executed evidence (2026-11-02): targeted exception/form regression **25/25**,
warehouse-permission follow-up **1/1**, final module regression **146/146**
(24 new exception tests and all 122 earlier tests), zero failures/errors. The native
claim-form regression caught during development was repaired and both affected
form tests passed. Real native done-move-line quantity correction was exercised.
Two independent concurrent refresh transactions committed against separate claims
for the same PO line: **one logical exception, one detection event**; one worker
retried after an expected lock conflict. Module update and exact pre-Step-07
reference/line/valuation/audit comparison passed. Results are retained in
`.testing/step07-results.json`; detailed logs remain in `.testing/logs`.
Native Purchase/Inventory roles remain separately required: APP 05 roles alone do
not grant access to confidential source documents. Step 07 ended before Step 08 development.


## Step 08 - Vendor billing source reconciliation

The native `account.move.line.purchase_line_id` and purchase line `invoice_lines`
relations are the source of truth. Targeted, access-aware batch reads support many
bills per PO, many PO lines per bill and repeated products on distinct PO lines.
No supplier/name/reference/date-only mapping is created. A bill header total is
never assigned to a PO or claim: native line quantities and amounts retain their
individual source attribution. Accounting remains authoritative; no second
accounting ledger or manual bill-mapping model is introduced.

### State, quantity and credit definitions

Only vendor bills (`in_invoice`) and vendor credits (`in_refund`) are relevant.
Physical quantity aggregation requires an actual linked product line, matching
product/company, a supported purchasing UoM and physical goods. Sections,
subsections, notes, tax/payment-term/balancing lines, down payments, services and
unattributed charges do not inflate physical billed quantities. These legitimate
financial charges remain visible through the original native bill.

Per exact PO line, quantities are separated into:

- Draft vendor bills.
- Gross posted vendor bills.
- Canceled vendor bills, historical only.
- Draft vendor credits.
- Posted vendor credits.
- Canceled vendor credits, historical only.

Native `qty_invoiced` can include draft bills and credit treatment; it is deliberately
not used as posted exposure. Native UoM conversion normalizes supported quantities
without intermediate rounding. Incompatible, negative, nonfinite or effectively-zero
positive conversions require review. Incomplete totals are placeholders, hidden in
the UI; API consumers must require `billing_complete` before using quantities.

Credits remain separate because their purpose may be price, tax, return, concession
or quantity correction. No effective net-posted-billed measure is asserted without
verified credit purpose. Native reversal relationships expose related credits even
when their line mapping needs investigation. Unrelated supplier credits are not
searched for or allocated. A missing purchase link within a related bill/reversal
is flagged, without guessing its quantity allocation or modifying native links.
Completely isolated bills without native PO/reversal provenance remain outside
this targeted scan and require manual investigation.

### Financial and relationship integrity

Bill supplier contacts are compatible only when they have the same actual
`commercial_partner_id` as the PO supplier. Similar names do not establish identity.
New forged cross-company bill-line/PO-line links are rejected by native company
checking and an explicit ORM constraint, including line moves between documents.
Existing inconsistent source records are not silently rewritten. Shared products
and partners remain supported through normal Odoo company semantics.

Each source bill keeps its own currency, unit price, discount, taxes, native untaxed
`price_subtotal` and tax-inclusive `price_total`. No posted amounts are recomputed,
no header totals are spread over POs, and no monetary sum or exchange-rate conversion
is introduced. A PO/bill currency mismatch requires review. Valid foreign-currency
sources remain in that transaction currency, independently of company currency.

When both physical and accounting evidence are complete and credit purpose is not
ambiguous, `max(gross posted billed - net supplier received, 0)` is shown as a
**POTENTIAL DIFFERENCE - NOT CONFIRMED CLAIM**. It is not supplier liability or
recoverable money. Draft billed quantities stay separate. The product's native
ordered/received billing-control policy is displayed: ordered-quantity billing may
legitimately precede delivery. Open backorders, delivery commitments and credit
purpose still require investigation. Returns are not subtracted again from billed
quantities, and credit notes are not treated as receipts or cash recovery.

### Reused review workflow and accounting security

The existing exception model/XML IDs and immutable audit architecture are reused.
`reconciliation_kind` separates receiving from billing; all old records default to
receiving. Independent refresh scopes prevent receiving scans from clearing billing
findings or the reverse. Billing categories include unmatched bill lines, posted or
draft billing above receipts, potential duplicate groups, supplier/currency/UoM
mismatches, credit-purpose review, canceled documents and submitted-source changes.

Stable source keys use company, PO/line, category and source/event IDs. Repeated
unchanged refreshes do not duplicate findings or audit events. Changed evidence
updates/reopens the same logical finding, preserving prior decisions. Absent
conditions are retained as history. Duplicate suspicion requires the same actual
company/commercial supplier, nonempty normalized invoice reference, invoice date,
currency and complete linked product/quantity/UoM/price/discount/tax details. One
review represents each matching group, anchored by its earliest document ID;
group growth updates that record rather than creating every possible document pair.
Different legitimate references or line details do not match. A suspected group is
never automatically canceled or declared fraudulent.

Purchase claim users and Accounts Reviewers may explicitly refresh billing review
when native source permissions allow it. Assigned purchase/accounts reviewers may
start review and record notes. Purchase Claim Managers and Finance Managers may
complete billing reviews; this is no financial claim approval. Finance-only roles
do not gain receiving-decision authority. Accounts write/create permissions on the
custom model are restricted to billing records. Existing receiving role policies
remain effective. Decisions require reasons and audit history remains server-created,
immutable and transaction-bound. Source accounting line IDs are retained in audit
context alongside the existing receiving evidence and prior outcomes.

APP 05 grants no native Accounting access. Actual Accounting ACLs and row rules are
mandatory; native Purchase permissions may already grant some bill visibility in
this installed source. Missing PO, bill, line or reversal access produces an explicit
unavailable basis, not zero posted exposure. Stored billing findings and their audit
history are hidden when their accounting evidence becomes inaccessible. Findings
that depend on physical quantities also retain stock-evidence access restrictions.
Company restrictions apply to reads, searches, counts, grouping, actions and review.
No business reads use unrestricted elevation.

### Native UI, refresh and source changes

The claim's Vendor Bill Reconciliation page separates accounting from physical
receiving and claim workflow. It shows state-aware quantities, purchase UoM,
receiving/backorder context, policy and review status. A 40-line preview and paginated
read-only evidence views show native dates, states, quantities, prices, discounts,
taxes, subtotals and currencies. Secure actions open only related authorized vendor
documents or line evidence. Standard Accounting form workflows/buttons are preserved.
The Billing Exceptions menu provides the existing assignment/review workbench.

Summaries are live, non-stored projections. **Refresh Billing Review** updates only
custom exceptions/audit history; it requires no write grant on the claim for an
otherwise authorized Accounts Reviewer. Native source edits/posting/cancellation/
reversals refresh the displayed categories. Submission records an access-aware
billing source fingerprint, not a duplicate financial ledger. Later material billing
changes warn on review claims and create a revalidation finding on explicit refresh,
without modifying proposed quantities, submitted valuation snapshots or claim state.
Old reviews without that baseline show an explicit limitation rather than invented
history. The scanner does not record every intervening draft edit and does not run
unbounded background/global accounting scans.

### Step 08 testing and boundaries

Reuse the existing disposable-database runner:

```powershell
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestVendorBillIntegration
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
& C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step08_upgrade.py
```

Tests use real native bill posting, valid journals/accounts and independent
non-superuser authorization assertions. The upgrade baseline includes prior claim
references, lines, valuation snapshots, receiving exceptions and both audit histories.
Detailed logs stay local; zero discovery or incomplete execution fails. No new
concurrency claim is made for Step 08; Step 07's separate evidence remains historical.

Step 08 does not establish confirmed supplier shortage liability. Step 08 does not
create final supplier claims automatically. Step 08 does not implement claim-specific
settlement allocations. Step 08 does not create vendor credit notes or refunds.
It never posts/cancels invoices, registers payments, reconciles accounts, changes
payables, changes purchase prices or updates Inventory. Standard native draft-source
deletion/correction remains possible; custom review/audit history preserves its
observed evidence IDs and outcomes without replacing the source accounting ledger.


Step 08 executed evidence (2026-11-02): complete billing-class targeted run
**28/28**, final duplicate-group focused check **2/2**, final module regression
**174/174** (all 146 earlier tests plus 28 billing tests), zero failures/errors.
Native posting, reversal, mixed draft/posted/canceled documents, multiple POs per
bill, tax modes, foreign currencies, UoM conversions and source access restrictions
were exercised. Native Accounting documents, amounts, balances, payments and
reconciliation records remained unchanged by APP 05 reads/refreshes. Module update
and the exact pre-Step-08 claim/line/valuation/receiving-exception/audit baseline
comparison passed. Evidence is retained in `.testing/step08-results.json`; full logs
remain local. No production database was used and Step 09 was not started.
If the earliest document leaves a suspected duplicate group, the successor anchor
is treated as a new occurrence; the former review and its outcomes remain in history.

## STEP 09 — Shortage detection review

Run **Run Shortage Detection** on a purchase order, or on explicitly selected purchase order lines using the Action menu. Open **Supplier Recovery ? Shortage Detection ? Detection Candidates** to investigate. Refresh Detection reruns only the selected review records' PO lines. There is no installation scan, automatic claim creation, or background historical scan.

Detection extends `supplier.shortage.receipt.exception` with `reconciliation_kind=detection`. It reuses the STEP 06 `_ssc_receiving_analysis` and STEP 08 `_ssc_billing_analysis` services, rather than native `qty_received` or `qty_invoiced` as gross receipt / posted-bill ledgers. Installed Odoo 20 purchase source confirms that native received quantities depend on return/refund flags, while native invoiced quantities include draft documents and subtract credits. The existing services preserve supplier boundary provenance, exclude internal transfers, normalize UoMs through the Odoo 20 reference hierarchy, and expose completeness and access failures.

Each PO line has one current detection snapshot, identified by company / PO-line ID / detection. Ordered quantity comes from that line; gross completed receipts, supplier returns and pending/backorder demand come from the receipt service; posted, draft and posted credit quantities come separately from the billing service. Net received = gross minus eligible returns. Physical difference = positive ordered minus net received. Potential posted billed excess = positive posted billed minus net received, only with complete compatible evidence. Quantities are compared with Odoo UoM precision. Drafts and canceled bills never inflate posted billing, and credits never automatically offset it. Values with `Evidence Complete` disabled are incomplete projections and must not be interpreted as verified zero balances.

Primary classification preserves secondary findings in `findings`. Active delivery demand takes precedence over a candidate. A closed-delivery physical and posted-billing difference proposes `min(physical difference, posted billed excess)` for human review. Canceled demand is retained as a separate finding, without attributing responsibility. Returns and posted credits require review and suppress candidate quantity. Unsupported physical basis, inaccessible sources, source conflicts and suspected duplicate invoices prevent candidate eligibility. Billing above original ordered demand is retained as an additional overbilling finding. Identical supplier reference/date/currency and exact linked line details trigger duplicate review; this is a heuristic, not proof of duplicate debt.

**Indicative Untaxed Exposure** is deliberately conservative: it requires complete candidate evidence, identical normalized posted bill-line untaxed unit prices agreeing with discounted PO pricing, no tax configuration, and no existing linked claims. Discounts are retained through posted untaxed subtotals. Different bill prices, purchase/bill price disagreement, tax-inclusive or other taxes, foreign-currency mismatches and uncertain allocation produce **Requires Review**, with no monetary estimate. No exchange rate is invented and no currencies are added together. This follows STEP 03's provisional valuation boundary; it does not change claim valuation snapshots. Tax-bearing detection valuation is not yet supported, even where STEP 03 can value an individual claim proposal.

The existing New / In Review / Resolved / Dismissed lifecycle is reused. **Verify Candidate Evidence** marks a complete candidate Resolved after checking current sources; this means candidate evidence reviewed only. Managers control verification, dismissal, reopening and assignment; a meaningful reason is required. Other authorized reviewers can start review and record notes. Source quantities and workflow state reject direct ORM writes. Refreshing changed evidence resets a completed or in-progress detection review to New. Historical decisions remain in immutable STEP 04 audit events, which now capture classification, findings, all detection quantity snapshots, candidate quantity and exposure. Unchanged scans add no audit events. Current reconciled snapshots remain visible with zero candidate quantity; they do not delete history.

Fingerprinting uses deterministic receipt/bill source signatures, PO quantity/pricing/state, claim references and allocations, classification and valuation. Explicit scans lock POs in ID order; the existing database unique source key is the final duplicate barrier. Scans do not write source stock or accounting fields. Linked existing claims flag possible overlap without globally prohibiting further nonoverlapping candidates; exposure is withheld pending allocation review. This is overlap awareness, not final allocation enforcement.

Existing global company/source access domains, stock/accounting evidence coverage checks and source drill-down permissions apply to detection. No elevated source reads are introduced. If a source model is unavailable, the action can fail closed rather than storing a result that would be inaccessible under the existing exception security rules. Users must refresh explicitly after source changes; there are no source-write triggers in STEP 09. The UI provides source links, quantities, findings, priority, responsible employee, review notes and audit history. Priorities use an evidence/category policy (high for source conflicts, incomplete access, candidates and above-order billing; medium for operational differences; low for reconciliation). Company settings provide high/critical monetary thresholds, evaluated only for reliable indicative exposure in company currency; zero disables a threshold. Contractual overdue assessment and supplier recurrence scoring are not implemented.

Targeted testing (existing disposable test database):

```powershell
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestShortageDetection
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
```

Detection candidates are NOT approved supplier claims. Indicative financial exposure is NOT verified recovery. Credit-note settlement allocation is NOT implemented. Supplier communication and formal approval are NOT completed in STEP 09. At the STEP 09 milestone, STEP 10 was not implemented; the following section describes its subsequent implementation.

STEP 09 standalone checks (same disposable database): `.testing/check_step09_concurrency.py` uses independent concurrent transactions and bounded retries, and validates reviewer views/actions through native Odoo APIs. `.testing/check_step09_upgrade.py` compares established historical claim, line, valuation and audit fixtures with their preserved baseline; later test fixtures are excluded from that comparison. Run these with the same verified virtualenv Python used above. Browser visual/responsive inspection is not automated by these server-side checks.

## STEP 10 — Reviewed candidate claim preparation

STEP 10 creates and prepares **Draft** claims, using the existing `supplier.shortage.claim` and `supplier.shortage.claim.line` models. It introduces no claim approval, supplier communication, receivable, stock transaction, credit note, payment, replacement or settlement workflow.

### Eligibility and preparation

Use **Supplier Recovery ? Shortage Detection ? Detection Candidates / Review Queue**. A purchase claim manager first verifies the internal candidate evidence. **Prepare Draft Claim** opens a quantity dialog; selecting multiple compatible candidates from the list Action menu prepares one multi-line draft. All selected candidates must belong to the same company, exact PO, supplier and transaction currency. Different PO lines remain separate even when their product is identical. The resulting claim opens in Draft and is never automatically submitted.

The server checks candidate type, resolved internal evidence review, positive quantity, complete receiving and posted-billing provenance, exact PO/line/product/UoM/company relationships, native source permissions, current source fingerprint, unresolved critical source findings and existing allocations. Pending delivery, cleared findings, unsupported goods, stale fingerprints and inaccessible evidence cannot bypass these checks through the dialog or direct ORM creation. Existing unallocated historical Under Review claims against the same PO line block new preparation until authorized reconciliation; their quantities are not silently assumed to be zero.

A source-only fingerprint reuses the STEP 06–09 receipt/billing services and native receiving integrity verification. It includes source document signatures, company/supplier/product/UoM/currency, quantity, pricing, tax mode/hierarchy amounts and inclusion rules, currency rounding, company tax rounding and PO state. STEP 10 excludes internal claim references, reservation revisions and overlap presentation from this source fingerprint: creating a legitimate partial claim must not invalidate unchanged physical evidence. Existing STEP 09 review signatures from the previous algorithm require explicit refresh and renewed verification before conversion. Historical audit records are retained.

### Allocation architecture and concurrency

Each candidate-backed claim line is the allocation record; no second allocation ledger is introduced. `detection_id` supplies stable source identity, `allocated_qty` stores the quantity in the **captured purchase UoM**, and `allocation_state` distinguishes Reserved and Released. Original preparation and current allocation bases are separate protected JSON snapshots. Public create, One2many commands, batch create and controlled edits all use the same checks. There is no global uniqueness rule on a purchase line.

For compatible current UoM bases:

`Available Quantity = max(Candidate Quantity - Active Reserved Quantity, 0)`

Draft and Under Review reservations remain active. A 15-unit candidate can support separate 10-unit and 5-unit claims; an additional 8 units when only 5 remain is rejected atomically. Claims and lines may be copied only as **unallocated incomplete drafts**: allocation links, evidence and protected snapshots are not copied, and submission requires new reviewed allocation. This preserves ordinary draft-copy functionality without duplicating reservations.

A claim user may correct an active draft quantity with a meaningful correction reason. Increases require fresh reviewed sources and available capacity. Reductions can progressively correct a stale over-reserved quantity; they do not acknowledge changed source evidence. A purchase claim manager may explicitly **Release / Exclude Line** in Draft with a reason. The line, original quantities, snapshots, evidence and audit remain; release is never inferred from inactivity. Unallocated manual lines can be explicitly excluded using the same audited policy. Under Review claims must first return to Draft. There are no final-state cancellation, rejection or settlement release rules in this step; future states must retain reservations until an explicit policy exists.

Locks use installed Odoo 20 `lock_for_update()` facilities, acquired one record at a time in sorted ID order. PO serialization precedes candidate and claim edits; candidate source rows and supporting evidence are locked during critical validation. Each reservation change versions the candidate row through `allocation_revision`. Under PostgreSQL Repeatable Read, a stale concurrent transaction must retry rather than use an obsolete allocation sum. Native lock/serialization errors require a fresh transaction; business methods never commit. A SQL constraint also requires active reservations to have a candidate and positive normalized quantity. Independent concurrent tests exercise both one-candidate and multi-candidate races, including rollback of losing headers, lines and audit events.

If the purchase UoM identity/factor no longer agrees with an existing reservation basis, the engine does **not** add incompatible quantities. Reservation UoM Basis Compatible becomes false, available quantity is withheld as zero/unknown, and Allocation Integrity Blocked is true. An authorized release and corrected preparation are required. A reduced candidate quantity below existing reservations is likewise a blocking integrity finding on the existing detection exception; reservations are not silently released.

### Manual draft compatibility

Existing manual drafts, submitted valuation snapshots and audit history survive upgrade. A manual draft can remain incomplete while investigation proceeds, but it cannot submit an unlimited or unallocated quantity. Enter a meaningful **Manual Preparation Exception Reason**, then use **Link Reviewed Candidate** for the exact existing PO line. This reserves its explicitly entered, normalized quantity without silently changing it. Source identity cannot subsequently be switched by editing. Unsupported manual lines may be excluded with an authorized reason; they remain in history. For a different PO, prepare a separate claim instead of rewriting an allocated historical line.

### Evidence and attachments

`supplier.shortage.claim.evidence` distinguishes typed references to existing purchase orders, receipt/return pickings and vendor documents from uploaded files. Supported evidence categories include PO, GRN, bill, challan, packing list, count sheet, photograph, supplier correspondence, supplier return, quality inspection and other supporting documents. Optional line association must belong to the exact parent claim. Native receipt and bill references must occur in that claim's candidate provenance; references cannot be guessed from a product name.

Uploads use native `ir.attachment` binary storage privately bound to the evidence record. A private server method creates the attachment only after evidence and parent authorization. Public attachment creation/rebinding cannot attach arbitrary existing private files to evidence. File contents and source associations are immutable: add a new evidence record for a corrected document. Evidence and its attachments cannot be deleted. Failed creation rolls back the evidence, attachment association and audit event together. Public links and access tokens are prohibited; native download/stream paths recheck the real user's parent evidence and source access, including company restrictions. No custom unrestricted download controller is introduced.

Company configuration controls allowed filename extensions, maximum file size and minimum verified evidence count per active claim line. Recovery configuration administrators use **Supplier Recovery ? Claim Evidence Policy** for their activated authorized company. The service permits only the three policy fields, enforces at least one verified item and a 1?100 MB file limit, and uses a narrowly scoped configuration write without granting company-wide or claim business write rights. Native company administrators can also use the company form. Defaults are PDF/PNG/JPG/JPEG/TXT/CSV, 10 MB and one verified item. Filenames are sanitized, limited to 255 characters, and accompanied by native attachment metadata. These are file-policy checks, **not malware scanning**. List views and counts do not fetch binary content. The evidence upload path creates no unattached temporary files.

Evidence states are Not Reviewed, Internally Verified, Rejected and Requires More Evidence. Only a purchase claim manager may change verification state, reviewer or timestamp through reasoned review actions. Ordinary users cannot forge these fields. Claim users may add suitable draft evidence and notes; warehouse contributors may add receiving/count/photo/quality evidence but cannot verify it or submit financial correspondence as a warehouse-only role. Accounts/finance reviewers inspect only evidence for which native source access permits reading. Configuration administrator membership does not grant business approval authority.

### Review checklist, source snapshots and corrections

Before **Submit for Internal Review**, each active line needs a reviewed current candidate, a compatible nonoverlapping reservation and the configured quantity of relevant, internally verified evidence. The claim needs a concise shortage reason. Candidate receiving and posted-bill provenance must remain complete. A real source reference can satisfy supporting evidence policy; a fabricated GRN is never required where none exists. A documented internal investigation of missing receipt evidence must remain in the candidate review and shortage reason. Rejected/unreviewed evidence does not satisfy submission.

Preparation snapshots record candidate reference/fingerprint, PO and line, company, supplier, product, captured UoM/factors, ordered/gross/returned/net quantities, posted billing, pending/backorder context, candidate and allocated quantities, source IDs, currency, source reviewer/date, and STEP 03 provisional valuation. They do not copy complete stock/accounting documents. STEP 03's native price/discount/UoM/tax valuation remains provisional; detection exposure is not substituted as an approved claim amount.

**Check Current Sources** explicitly refreshes selected detection sources and exposes changed review/quantity positions. Submission and conversion independently validate live sources even before a refresh. Material changes block submission; existing snapshots are preserved. After correcting quantities and obtaining renewed candidate verification, a manager can **Accept Renewed Source Review** in Draft with a reason. This updates the current allocation basis and audits the change while preserving the original preparation snapshot. Review submission captures the existing protected valuation snapshots and source signatures; authorized return to Draft preserves prior review history. Failed transactions leave no successful submission audit event. Automatic source-change scheduling is not introduced.

The native claim form includes preparation status, shortage/manual/correction reasons, candidate references, reserved and available quantities, source snapshots, evidence review, release/link actions and existing audit history. The detection workbench displays active reservations, availability, UoM compatibility, blocking integrity status and existing allocated claims. Indicative detection exposure is withheld when represented by active claims; it is never added as recovered money. Readiness shown on the form is a preparation indicator; submission performs the authoritative source, quantity and evidence validation.

### Security, audit and testing

Global company restrictions extend to evidence and source-backed lines. Source document permissions constrain searches, group results, drill-downs, snapshots and attachment downloads. Hidden allocation/claim/source findings make eligibility or availability incomplete instead of being read as zero. No unrestricted elevated source reads are introduced. Claim preparation audit snapshots require access to the candidate-backed lines whose billing data they contain; header access alone does not expose those snapshots. Warehouse/configuration-only evidence readers see receiving/PO categories, while financial correspondence remains restricted to the claim/accounts roles with applicable native source access. The existing narrowly scoped immutable audit insertion service is reused for claim preparation, source linkage/reservation, quantity correction/release, renewed source review, evidence addition/notes/verification, submission and return. Actor, timestamp, company, source IDs, quantities, reasons and original snapshots remain traceable. Failed transactions roll back success events; unchanged scans do not generate repeated material-source events.

Use the existing isolated disposable database and low-token runner:

```powershell
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestClaimPreparation
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step10_concurrency.py
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step10_upgrade.py
```

`check_step10_upgrade.py --capture` captures an established historical baseline before a subsequent update; verification excludes later new test fixtures but checks every captured reference and snapshot. The older STEP 01–09 positive submission fixtures now obtain real posted bills, reviewed source candidates, explicit manual allocations and verified evidence through public APIs before submitting. Negative permission and direct-ORM validation tests remain active; no production validator is patched or disabled. Full logs remain local under `.testing/logs`, with concise outcomes in the STEP 10 result artifacts. A zero-test run is failure. Browser visual/responsive inspection remains separate from server-side view compilation.

**Draft Claim != Approved Claim. Reviewed Candidate != Supplier Acceptance. Provisional Claim Amount != Financial Recovery. Claim Quantity Allocation != Settled Quantity. Credit Note and Replacement Settlement are not implemented in STEP 10.** Claim approval, supplier communication and financial recovery remain future workflow events; STEP 11 extends internal evidence verification as described below.

Odoo 20 source limitation verified during STEP 10 QA: a PO UoM correction can leave native stored `qty_received` stale until the native receiving computation runs again. Detection blocks the resulting native/source conflict and never repairs source documents itself. The correction regression fixture invokes Odoo?s native received-quantity computation explicitly; done/posted states are produced only through standard stock/accounting workflows.

STEP 10 final verified outcome: 22 targeted preparation tests and 210 complete module regression tests executed and passed, zero failures/errors. Both independent allocation race cases passed (one candidate and two candidates), with one successful allocation and one rejected request in each case and no orphan business/audit rows. Historical preservation passed for 19 established claims, 20 lines and 43 audit events. Module updates succeeded. Full details are in `.testing/step10-results.json`; browser visual/responsive QA remains NOT VERIFIED.


## STEP 11 ? Internal evidence verification and readiness

Existing claim evidence rows now represent immutable, linked document revisions. Each item has a root, revision number, current flag, superseded reference, submitted actor/date, review conclusion, and reviewed source fingerprint. Replacing a document creates a new evidence row and private attachment; the old file, rejection reason, checker and timestamp remain traceable. A checksum detects content differences; it does not prove authenticity. New revisions start unreviewed.

Evidence categories cover purchase terms; receipts, counts, packing/challans, photographs, quality and lot records; bills, bill lines, credits and accounting notes; supplier correspondence and internal statements. Typed native references validate the exact PO, PO line, picking, stock move, bill or bill line using normal source access. Files are not copied from source documents. Human statements remain labelled text, never fabricated GRNs. Line-specific evidence supports only that line. Explicit scope selects additional legitimate lines; a native PO reference may cover its own claim lines. Unscoped uploaded files do not prove every line. Duplicate file checksums are not counted as independent proofs.

### Private files and independent review

Upload through the evidence form after selecting an accessible draft claim. Native Odoo attachments belong to the evidence record and company, remain private, and cannot receive public flags or sharing tokens. Direct replacement, rebinding and deletion are blocked. Normal parent/source permissions apply to reads and native streaming. Company settings control maximum size and allowed extensions; content inspection additionally limits uploads to PDF, JPEG, PNG and policy-enabled UTF-8 TXT/CSV. Active SVG/HTML and content/extension mismatches are rejected. There is no malware scanner, OCR, signature validation or automated authenticity guarantee.

Odoo 20 removed `ir.attachment.datas`; uploads now use its supported `raw` binary API. Tests check actual stored bytes rather than attachment metadata alone. Older metadata-only attachments cannot have missing bytes reconstructed: retain the history and upload a valid new revision for independent review. Verification rejects empty or unsupported content.

Preparers submit documents; independent warehouse reviewers check physical evidence, accounting reviewers check financial evidence, and purchase claim managers check other evidence. Native source access remains necessary. The submitting employee cannot verify their own evidence. Configuration administrators do not gain business review rights automatically. Controlled actions record the checker, date, revision, source basis and conclusion. Rejected evidence retains its reason; corrected content needs a new revision and review. Review assignment does not confer access or approval authority.

### Warehouse observations

A delivery-specific observation records the exact claim line/product, attributable receipt/warehouse/location where available, count method/date, quantity/UoM, explanation, recorder and supporting documents. Only an independent warehouse reviewer can verify it. A replacement retains the previous observation. Company-wide on-hand stock is not delivery evidence.

An additional-unrecorded-goods observation may show an indicative supported quantity, but any contradiction blocks readiness for native-source reconciliation. For example, five newly observed units against a fifteen-unit candidate can show ten indicative units while native receipts, candidate quantities and existing allocations remain unchanged. Excess reservations require authorized correction; this module never adjusts stock, shrinks reservations or posts accounting transactions.

### Requirements, readiness and submission

Choose the claim evidence scenario: billed/unreceived, backorder dispute, warehouse error or return/quality. Core requirements include valid reviewed source allocations, current fingerprints, purchase/receiving provenance and independently verified warehouse observations. Billed/unreceived additionally needs verified posted billing evidence; quality and backorder cases add relevant inspection or communication requirements. Uploaded files or credits alone do not establish posted bill provenance. Company-specific supplemental requirement policies may add categories. An independent manager may authorize a documented, source-bound exception only where the policy allows; core source/quantity/warehouse requirements cannot be waived.

Use **Evaluate Evidence Readiness** to obtain deterministic line checklists and actionable messages. Outcomes are Incomplete, Needs Verification, Needs Correction, Blocked by Source Issue, or Ready for Internal Review. Rejected mandatory evidence, stale verification, current unreviewed replacements, missing observations and source conflicts cannot satisfy readiness. All active claim lines must qualify. Restricted source data produces an access-incomplete result, not a false zero or green checklist. Readiness fields are protected server-side.

Existing submission actions run the same readiness gate inside the claim transaction after source/allocation validation. A failure leaves the claim in Draft without a successful submission audit. Direct state writes remain prohibited. Changes to line quantities, relevant native receipt/bill/PO facts, evidence revisions, or policy settings invalidate scoped readiness. Dependency-specific source changes mark affected verification stale; billing-only changes do not invalidate unrelated physical documents. Historical review snapshots are preserved. Refreshing readiness never changes submitted amounts or source records.

### Workspace, audit and operations

The claim form includes Readiness, line checks, evidence scope/revisions and warehouse observations. Supplier Recovery ? Evidence Management provides Evidence Reviews, Missing Evidence and company-scoped requirement configuration. Native source/document buttons recheck permissions. History records revision creation, reviews/rejection, observations, authorized exceptions and material readiness changes through the existing immutable audit service. Repeated unchanged evaluation does not duplicate audit events. No binary contents enter audit logs.

Metadata invalidation uses bounded identity joins to affected claim lines, without elevated source reads or full historical stock/account scans. Multi-company rules protect policies, exceptions, observations, evidence, revisions and audit visibility. Existing allocation locks and source fingerprint checks remain authoritative; evidence observations are not a second inventory ledger.

```powershell
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestClaimEvidence
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step11_upgrade.py
```

Capture a historical baseline with `check_step11_upgrade.py --capture` before a repeat upgrade. Test logs remain in `.testing/logs`; zero execution is failure. Browser visual/responsive inspection is separate from server view compilation.

**Internal Evidence Verification != Supplier Claim Acceptance. Readiness for Internal Review != Final Claim Approval.** Draft quantities, reservations and provisional amounts are not recoveries. Supplier communication, liability approval, credit allocation, replacement settlement and financial recovery are not implemented in STEP 11.

## STEP 12 — Internal company claim approval

Internal Approval != Supplier Acceptance. Approved Claim Amount != Recovered Amount.
Approval Policy != Accounting Entry. Approved Claim != Posted Receivable.
Supplier submission and settlement are future steps and are not invoked by these actions.

### Policy and financial authorization

` supplier.shortage.approval.policy ` contains company-specific amount bands, effective dates,
calendar-day deadlines and a configured Purchase, Purchase/Accounts, or Purchase/Accounts/Finance
chain. Only Module Administrators may configure policies, within their activated companies.
No default monetary limits are installed. Missing coverage fails closed with a configuration-gap
message. Bands use the company currency: `(minimum, maximum]`; minimum zero includes zero,
and maximum zero means unbounded. Active bands cannot overlap during intersecting effective dates.
Currency-aware comparison handles exact boundaries. Archive policies instead of deleting them.

Accounts must explicitly verify the current financial value, with a justification, after evidence
review submission. A claim maker, responsible preparer or review submitter cannot perform this
verification. The reviewed basis prorates the native posted bill line's **untaxed subtotal** by
reserved claim quantity in the bill UoM; it retains invoice and purchase lines, quantities, UoM
factors, unit price, discount, tax identifiers, transaction currency, reviewer and review time.
The STEP 03 provisional estimate never becomes verified merely by submitting or approving.
No posted bill, tax computation, stock record or accounting entry is rewritten.

This initial valuation implementation deliberately blocks ambiguous split invoice lines, posted
credit context, incompatible UoMs, quantities exceeding posted billing, supplier/company/currency
conflicts and absent posted pricing. These need Accounting reconciliation before approval; there
is no manual override that invents a financial amount. Native posted subtotals already include
Odoo's discount and tax-exclusion treatment.

For foreign-currency claims, threshold comparison uses the submission date and Odoo's native
conversion API. Positive, finite documented rates must exist on/before that date for both
currencies, in the company root or global rate scope. The cycle freezes original amount,
company-currency comparison, conversion date, rate and source rate rows. Later exchange-rate
updates do not reinterpret the existing authorization; a new cycle obtains a new conversion basis.

### Workflow, maker-checker and assignment

Existing Draft and Under Review lifecycle values remain intact. A separate protected
`approval_status` records awaiting Purchase/Accounts/Finance, internally approved, rejected,
returned and stale outcomes. Prepare and independently verify evidence, submit for review,
use **Financial Review / Correction ? Verify Financial Value**, then **Request Internal Approval**.

Purchase Claim Managers perform the commercial stage, Accounts Reviewers the financial stage,
and Finance Managers the high-value authorization stage when required by the frozen policy.
Every action checks authenticated identity, activated company, record access, exact role,
current cycle/stage, prerequisites, maker-checker and assignment. Financial reviewers also need
native Purchase, Stock and Accounting source visibility. Business decisions reject sudo mode.
Accounts workflow CRUD access does not grant claim preparation or line-editing authority.

The creator, responsible preparer and review submitter cannot approve, regardless of group overlap.
Independent-stage policies additionally prohibit any previous approver from deciding a later stage.
The separation option is frozen per cycle; disabling it is an explicit configuration choice,
never a self-approval waiver. Existing evidence verification still enforces its own independent
checker controls. Evidence uploaders/checkers and warehouse reviewers are captured in the snapshot;
participation in evidence review alone does not disqualify an otherwise independent claim approver.

Pending stages initially use the authorized role pool. Module Administrators with visibility
of the claim and its native sources can reassign a pending stage using the review dialog.
A reason, compatible role, company and maker-checker eligibility are mandatory. Reassignment logs
old/new identities and grants no decision. There is no organization-wide delegation engine.
Deadlines and request age are informational calendar-day values; working calendars, scheduled
reminders, notifications, automatic escalation and automatic approval are not implemented.

### Rejection, correction, reapproval and audit

Only the eligible current-stage approver can reject or return, with a meaningful reason.
Later waiting requests become superseded; completed decisions remain unchanged. Quantity
allocations stay reserved. A Claim Manager opens draft correction using the review dialog and
records a reason; financial verification is cleared. Correct the draft, reverify affected evidence,
resubmit, obtain Accounts valuation verification and request a new cycle. The applicable monetary
band is selected again; old decisions cannot authorize the new cycle. Historical claims are not deleted.

Targeted existing source hooks invalidate pending/final internal authorization after receipt,
billing, quantity, evidence, candidate, UoM, tax or relevant evidence-policy changes. Current status
becomes **Reapproval Required**; historical cycle snapshots and decisions remain available. Unrelated
claims are not invalidated by a source change scoped to another PO line. Current approval actions
also rerun readiness, source fingerprints, allocation totals and financial-basis comparison.

` supplier.shortage.approval.cycle ` freezes the commercial/source basis, policy, original and
comparison amounts, conversion, evidence status and reviewer identities. Protected decision rows
in ` supplier.shortage.claim.approval ` identify stage, actor, time, reason and cycle. Reused STEP 04
audit events record requests, selected chains/policies, decisions, final authorization, reassignment,
correction and invalidation. Supersession is traceable through cycle number/state; completed decisions
are not silently changed. These records are not digital signatures or an accounting ledger.

Public create/write/unlink/copy and parent One2many mutation cannot fabricate decisions or snapshots.
Private insertion-only elevation follows business validation; no elevated source reads or authorization
bypass are exposed. Company/parent/source access applies to searches and grouped data. Native row locks,
cycle/stage uniqueness, atomic savepoints and current-cycle checks prevent duplicate decisions and
partial successful audits. Business methods never commit. Independent-transaction races use the
existing disposable test DB; sequential ORM calls are not described as concurrency testing.

### Approval Center and QA

Supplier Recovery ? Approvals provides My Pending Approvals (eligible role pool or assignment),
All Approval Requests, High-Value Claims (Finance-required cycles), Returned for Correction,
Approval History and administrator-only Approval Policies. Native list/form/search views show
supplier, company, PO, reviewed amount/currency, stage, assignee, requested date, age/deadline and
status. The claim form includes financial provenance, approval journey and frozen historical cycles.
No separate JavaScript application or executive recovery dashboard is introduced. Native view
compilation/access is tested; browser visual/responsive inspection is a separate manual check.

Use the existing virtual environment and allowlisted disposable DB, never production:

```powershell
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestClaimApproval
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/run.py --mode update --level regression
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step12_concurrency.py
C:/PycharmProjects/odoo20/.venv/Scripts/python.exe .testing/check_step12_upgrade.py
```

The STEP 11 historical baseline was captured before the STEP 12 schema upgrade. The upgrade check
compares original IDs, claim fields, line snapshots, evidence, candidate reviews and immutable audits.
The concurrency harness commits test fixtures/results only in its explicit disposable DB and uses
independent cursors/transactions for simultaneous approval and material-source-change races.
Complete logs remain in `.testing/logs`; zero discovered/executed tests is failure.
No STEP 13 work is included.
STEP 12 QA scheduling note: run TransactionCase suites and the standalone concurrency harness serially. Native company/group/attachment fixtures share registry data even when business claims use isolated companies. The harness also verifies concurrent overlapping policy configuration using a versioned company-row lock; it changes only company write-date metadata for serialization. Approval conversion uses the UTC server submission date and records the actual rate/date selected by the installed Odoo API; future/default fallback rates are rejected. Approval status mutations disable automatic chatter notifications, including notifications to external supplier followers; explicit immutable audit events remain authoritative.
A Claim Manager may also explicitly revoke a final internal authorization for documented draft correction. This marks current authorization stale before editing and clears financial verification; it retains the original approved decision and frozen snapshot. Fresh source/evidence review, Accounts valuation and a new approval cycle are required.

### STEP 12 verified QA result — 2026-11-03

PASS on the allowlisted disposable database `ssc_claim_test_20_step01_20261102`:
focused approval/company-security tests **32/32**, final UI-state/source/currency follow-up
**4/4**, and complete module regression **261/261**, with zero failures/errors in these final runs.
All **3** independent-transaction concurrency scenarios passed: duplicate decisions,
approval versus material source change, and overlapping policy creation from an older snapshot.
The policy race produced a SerializationFailure retry followed by rejection of the overlapping band.
Module update and historical comparison passed: **27 original claims, 32 original lines and
103 original audit events** retained their captured history, evidence and source snapshots.
Machine-readable results: `.testing/step12-results.json`; full logs: `.testing/logs`.
Earlier failed/interrupted runs are retained for diagnosis and are not counted as successful runs.
Native view compilation/access and the review wizard are tested. Browser visual/responsive review
is **NOT VERIFIED**. STEP 12 ends here; STEP 13 is described below.

## STEP 13 — Supplier submission and communication

Submission uses a separate versioned record. It requires current final internal approval, its
unchanged financial snapshot, current verified evidence and consistent quantity allocations.
Preparation, preview and opening a form never send email. Submission operations read native
Purchase, Stock and Accounting documents without changing them.

### Configuration and authority

Under **Supplier Communication → Configuration**, configure an active company policy, native
mail template, package count/size limits and supplier-specific claim contact. Configure the company
email identity and native outgoing server. Recipient and CC contacts must belong to the supplier's
commercial entity and be shared or belong to the claim company. Missing addresses are not guessed.
Arbitrary CC/BCC and sender impersonation are blocked; the actual envelope is preserved.

Sending requires **External Claim Sender** plus Purchase Claim Manager authority; configured
high-value Finance approval chains require Finance authority instead. Generic write access,
warehouse membership, configuration access or overlapping groups do not grant send authority.
Module administrators can configure native templates without gaining business send authority.
Templates are trusted administrator configuration; only subject/body are rendered, under a guard
against changing claim workflows during template rendering.

### Review and explicit send

1. Prepare a submission from an internally approved claim.
2. Approve relevant verified file evidence for external sharing, with a reason. Internal and accounting
   notes cannot be shared. Mandatory files must be selected; stale, rejected, empty, unrelated or
   cross-company documents are blocked.
3. Review the supplier contact, To/CC, subject, body and selected files in the send wizard.
4. Preview the PDF and exact file list. Editing content or selection clears confirmation and requires
   a fresh preview.
5. Explicitly confirm and queue the package. Server checks run again at confirmation and dispatch.
   Canceling the wizard sends nothing.

The Submission Center provides ready claims, history, failures, responses and follow-ups.
Native forms, search/group results, reports and downloads enforce company and source access.
Browser visual/responsive behavior remains **NOT VERIFIED**.

### Claim statement and evidence snapshots

The **Supplier Shortage Claim Statement** uses Odoo 20 Community's `base_report_wkhtmltox`
QWeb PDF engine, a self-contained A4 layout and the installed wkhtmltopdf runtime. PDF generation
must succeed before queueing. The revision freezes company branding, logo/address, supplier,
approved quantities, UoM, effective untaxed unit values, discount basis, currency and line totals.
The report includes receiving context, an evidence index, response instructions, repeated table
headings and page numbering. User text is escaped. Taxes, receivables, signatures and supplier
agreement are not invented. Current PO prices never replace approved snapshot values.

PDFs and selected supporting files are private protected byte snapshots. Historical printing returns
stored PDF bytes rather than rendering current source data or branding. Checksums identify content;
they are not digital signatures. Original evidence access remains required, so frozen copies cannot
bypass document permissions. No public-token download links are generated.

Packages are deduplicated and preserve an explicit evidence/version manifest. Company count/byte
limits and the native mail size limit, including estimated overhead, are checked before queueing
and dispatch. Oversized packages are blocked instead of omitting files or using public token links.

### Queue status, failures and retry

`draft` is unconfirmed preparation; `ready` is validated preview; `queued` means native mail accepted
the request; `sent` means successful outgoing processing. `failed` needs attention; `canceled` is
explicit withdrawal. Older sent revisions can be marked superseded without changing their original
documents or result. Delivery remains **unknown**: no provider delivery receipt, read tracking or
automatic supplier acknowledgment is implemented.

Native `mail.mail` and `mail.message` references are retained. Narrow privileged queue operations
follow real-user authorization and document checks. Displayed failures do not expose SMTP
credentials. Retry requires a reason and explicit acknowledgment of uncertain delivery, rechecks
the claim/package and reuses the same mail and PDF instead of inventing another claim revision.

Claim locking and unique request/revision keys prevent duplicate preparation and queueing. A
durable technical marker reserves each explicitly authorized dispatch attempt in an isolated
transaction before SMTP. It contains no document binaries, recipients or financial values; claim
business actions never manually commit business records. A crash with uncertain SMTP outcome
blocks blind redispatch until an authorized retry decision. SMTP cannot guarantee exactly-once
delivery.

### Responses, revisions and audit

Authorized staff can record supplier responses, acknowledgment, disagreement, requests for
information, phone calls and manual notes against sent submissions. Automatic inbound linking is
not configured; use secure manual recording. Message keywords never authorize supplier liability.

Explicit follow-ups and corrected revisions retain the prior submission reference and require fresh
preview/confirmation. Material quantity, value, evidence or source changes require STEP 12
reapproval before sending. Earlier PDFs, evidence, decisions and communications remain intact.
Existing audit infrastructure records sharing, confirmation, queueing, sending, failure, retry, revision
and response events. Direct ORM/RPC fabrication, modification and deletion of protected history
are blocked.

**Internal Approval != Supplier Acceptance. Email Sent != Email Delivered.**
**Email Delivered != Claim Accepted. Supplier Acknowledgment != Settlement.**
**Claim Submission != Financial Recovery.** Supplier dispute decisions, acceptance, settlement,
credit/replacement/refund processing and scheduled reminders belong to later steps.

### Low-token QA

Use the existing isolated disposable database, never production:

```powershell
python .testing/run.py --mode update --level targeted --tags /supplier_shortage_claim_reconciliation:TestSupplierSubmission
python .testing/run.py --mode update --level regression
python .testing/check_step13_concurrency.py
python .testing/check_step13_upgrade.py
```

Run concurrency checks serially after TransactionCase suites. The isolated-transaction harness
cancels its committed test queue rows afterward. SMTP entry points are mocked or prohibited;
PDF tests use the actual runtime. Logs are retained in `.testing/logs`; rendered review artifacts
are in `.testing/artifacts`.

### STEP 13 verified results

Final related regression: **295/295 passed**. Final document-access checks: **3/3 passed**.
These final runs executed 298 cases covering **296 unique tests**, including all 35 submission
test methods, with zero failures/errors. Two genuine isolated-transaction races passed: concurrent
Send produced one mail and one queue audit event; concurrent source change blocked stale sending.
Upgrade comparison preserved **34 original claims, 39 original lines and 232 original audit events**,
including captured approvals, evidence and source references. Actual four-page PDF rendering and
visual review passed. SMTP was mocked/prohibited. Machine-readable evidence:
`.testing/step13-results.json`; prior failed diagnostic runs are excluded from these counts.
Browser visual/responsive review remains **NOT VERIFIED**. STEP 14 has not been started.
