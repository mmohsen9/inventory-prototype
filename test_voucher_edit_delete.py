import os
import unittest
from datetime import date
from app import app, db
from models import (
    User, Location, Item, PurchaseTransaction, PurchaseLine,
    TransferOrder, TransferLine, StockBalance, StockMovement,
    SyncQueue, SyncLog, record_stock_movement
)

class TestVoucherEditDelete(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Users
        self.admin = User.query.filter_by(role="admin").first()
        self.accountant = User.query.filter_by(role="accountant").first()
        self.branch_staff = User.query.filter_by(role="branch_staff").first()
        self.warehouse = Location.query.filter_by(type="warehouse").first()
        self.branch = Location.query.filter_by(type="branch").first()
        self.item = Item.query.first()

    def tearDown(self):
        self.ctx.pop()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

    def test_purchase_edit_and_delete_by_accountant(self):
        # 1. Create a draft purchase
        p = PurchaseTransaction(
            location_id=self.warehouse.id,
            supplier_name="Test Supplier A",
            invoice_number="INV-TEST-001",
            invoice_date=date(2026, 10, 1),
            created_by=self.admin.id,
            status="draft"
        )
        db.session.add(p)
        db.session.commit()

        line = PurchaseLine(purchase_id=p.id, item_id=self.item.id, quantity=10.0, unit_cost=5.0)
        db.session.add(line)
        db.session.commit()

        # 2. Branch staff should NOT be able to edit header or delete
        self.login(self.branch_staff)
        res = self.client.post(f"/purchases/{p.id}", data={
            "action": "edit_header",
            "supplier_name": "Hacked Supplier"
        }, follow_redirects=True)
        db.session.refresh(p)
        self.assertEqual(p.supplier_name, "Test Supplier A")

        res = self.client.post(f"/purchases/{p.id}", data={
            "action": "delete_purchase"
        }, follow_redirects=True)
        self.assertIsNotNone(PurchaseTransaction.query.get(p.id))

        # 3. Accountant CAN edit header
        self.login(self.accountant)
        res = self.client.post(f"/purchases/{p.id}", data={
            "action": "edit_header",
            "supplier_name": "Updated Supplier B",
            "invoice_number": "INV-UPDATED-002",
            "invoice_date": "2026-10-04",
            "location_id": str(self.branch.id)
        }, follow_redirects=True)
        db.session.refresh(p)
        self.assertEqual(p.supplier_name, "Updated Supplier B")
        self.assertEqual(p.invoice_number, "INV-UPDATED-002")
        self.assertEqual(p.location_id, self.branch.id)

        # 4. Accountant CAN update line
        res = self.client.post(f"/purchases/{p.id}", data={
            "action": "update_line",
            "line_id": str(line.id),
            "new_quantity": "25.0",
            "new_cost": "6.5",
            "new_notes": "Updated by accountant"
        }, follow_redirects=True)
        db.session.refresh(line)
        self.assertEqual(line.quantity, 25.0)
        self.assertEqual(line.unit_cost, 6.5)

        # 5. Accountant CAN delete the purchase
        p_id = p.id
        res = self.client.post(f"/purchases/{p_id}", data={
            "action": "delete_purchase"
        }, follow_redirects=True)
        self.assertIsNone(PurchaseTransaction.query.get(p_id))
        self.assertIsNone(PurchaseLine.query.filter_by(purchase_id=p_id).first())
        print("[PASS] Purchase edit and delete by accountant passed!")

    def test_transfer_edit_and_delete_with_stock_reversal(self):
        # Initial stock balance
        bal_before = StockBalance.query.filter_by(item_id=self.item.id, location_id=self.warehouse.id).first()
        qty_start = bal_before.quantity if bal_before else 0.0

        # 1. Create a transfer
        t = TransferOrder(
            from_location_id=self.warehouse.id,
            to_location_id=self.branch.id,
            created_by=self.admin.id,
            status="draft"
        )
        db.session.add(t)
        db.session.commit()

        line = TransferLine(transfer_id=t.id, item_id=self.item.id, qty_requested=15.0, qty_sent=15.0)
        db.session.add(line)
        db.session.commit()

        # Send it (deducts 15.0 from warehouse)
        self.login(self.admin)
        self.client.post(f"/transfers/{t.id}", data={"action": "send"}, follow_redirects=True)
        db.session.refresh(t)
        self.assertEqual(t.status, "in_transit")

        bal_sent = StockBalance.query.filter_by(item_id=self.item.id, location_id=self.warehouse.id).first()
        self.assertAlmostEqual(bal_sent.quantity, qty_start - 15.0)

        # 2. Accountant updates the line quantity sent from 15 to 20
        self.login(self.accountant)
        self.client.post(f"/transfers/{t.id}", data={
            "action": "update_line",
            "line_id": str(line.id),
            "new_qty_sent": "20.0",
            "new_notes": "Modified by accountant"
        }, follow_redirects=True)
        db.session.refresh(line)
        self.assertEqual(line.qty_sent, 20.0)

        bal_updated = StockBalance.query.filter_by(item_id=self.item.id, location_id=self.warehouse.id).first()
        self.assertAlmostEqual(bal_updated.quantity, qty_start - 20.0)

        # 3. Branch staff cannot delete
        self.login(self.branch_staff)
        self.client.post(f"/transfers/{t.id}", data={"action": "delete_transfer"}, follow_redirects=True)
        self.assertIsNotNone(TransferOrder.query.get(t.id))

        # 4. Admin deletes the transfer -> Stock MUST be refunded to warehouse!
        self.login(self.admin)
        t_id = t.id
        self.client.post(f"/transfers/{t_id}", data={"action": "delete_transfer"}, follow_redirects=True)

        self.assertIsNone(TransferOrder.query.get(t_id))
        self.assertIsNone(TransferLine.query.filter_by(transfer_id=t_id).first())

        bal_refunded = StockBalance.query.filter_by(item_id=self.item.id, location_id=self.warehouse.id).first()
        self.assertAlmostEqual(bal_refunded.quantity, qty_start)
        print("[PASS] Transfer edit, line update, delete, and stock reversal passed!")

if __name__ == "__main__":
    unittest.main()
