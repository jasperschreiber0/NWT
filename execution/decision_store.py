"""Update a ticket's engine state without assuming a full unique index.

Production has a dated partial unique index, so a bare ON CONFLICT column
target cannot infer it. The state transition works with either index shape.
"""
def write_execution_decision(conn, ticket_id, decision, reasoning):
    with conn.cursor() as q:
        q.execute("UPDATE nwt_ticket_decisions SET decision=%s,reasoning=%s WHERE ticket_id=%s AND decided_by='EXECUTION_ENGINE'",
                  (decision,reasoning,ticket_id))
        if q.rowcount==0:
            q.execute("INSERT INTO nwt_ticket_decisions(ticket_id,decision,reasoning,decided_by) VALUES(%s,%s,%s,'EXECUTION_ENGINE') ON CONFLICT DO NOTHING",
                      (ticket_id,decision,reasoning))
            # Another worker may have inserted between UPDATE and INSERT.
            q.execute("UPDATE nwt_ticket_decisions SET decision=%s,reasoning=%s WHERE ticket_id=%s AND decided_by='EXECUTION_ENGINE'",
                      (decision,reasoning,ticket_id))
