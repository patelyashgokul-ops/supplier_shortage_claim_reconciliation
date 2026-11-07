from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    claims = env['supplier.shortage.claim'].search([])
    basis = {claim.id: {str(line.id): line.valuation_snapshot for line in claim.claim_line_ids if line.valuation_snapshot} for claim in claims}
    claims._audit('audit_enabled', env._('Immutable audit logging enabled for this pre-existing claim. Earlier history remains in chatter; no historical events were reconstructed.'),
                  reason=env._('Module upgrade to Step 04.'), basis=basis)
