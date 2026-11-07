from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # Step 01 had no claim date: use existing creation metadata, not upgrade day.
    for claim in env['supplier.shortage.claim'].search([]):
        claim.write({'claim_date': fields.Date.to_date(claim.create_date)})
    # Legacy proposed quantities remain unset for deliberate draft correction.
