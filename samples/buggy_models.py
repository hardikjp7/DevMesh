"""
Sample buggy file: N+1 query problem.
Use this to test that the pipeline flags SUGGESTION/WARNING-level issues correctly.
"""


class Order:
    def __init__(self, id, customer_id):
        self.id = id
        self.customer_id = customer_id


def get_customer(customer_id):
    # simulates a DB call
    return {"id": customer_id, "name": f"Customer {customer_id}"}


def get_all_orders():
    # simulates a DB call
    return [Order(i, i % 10) for i in range(100)]


def build_order_report():
    orders = get_all_orders()
    report = []
    for order in orders:
        # BUG: N+1 query — fetches customer one at a time inside the loop
        # instead of batching a single query for all needed customer_ids.
        customer = get_customer(order.customer_id)
        report.append({"order_id": order.id, "customer_name": customer["name"]})
    return report
