# Thin wrapper over the Razorpay SDK (test mode only).
# create_payment_link(amount_paise, reference_id, customer, notes) -> link
# fetch_payment_link(link_id)
# NOTE: amounts are handled in PAISE here; convert at the boundary, exactly once.
# TODO
