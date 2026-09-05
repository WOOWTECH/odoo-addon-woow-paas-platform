-- #864 PR-F pre-flight.  Read-only; each statement only returns a group count.
-- Run before upgrading a database that will create either PR-F partial UNIQUE index.

-- A. Duplicate wallet credits for one payment transaction.
SELECT COUNT(*) AS duplicate_key_groups
FROM (
    SELECT payment_transaction_id
    FROM wallet_transaction
    WHERE transaction_type = 'topup'
      AND payment_transaction_id IS NOT NULL
    GROUP BY payment_transaction_id
    HAVING COUNT(*) > 1
) AS duplicate_groups;

-- B. One MerchantTradeNo projected to multiple existing top-up invoices.
SELECT COUNT(*) AS duplicate_key_groups
FROM (
    SELECT pt.reference
    FROM woow_wallet_topup AS wt
    JOIN payment_transaction AS pt ON pt.id = wt.payment_transaction_id
    WHERE wt.invoice_account_move_id IS NOT NULL
      AND pt.reference IS NOT NULL
    GROUP BY pt.reference
    HAVING COUNT(DISTINCT wt.invoice_account_move_id) > 1
) AS duplicate_groups;

-- C. One existing invoice projected from multiple MerchantTradeNo values.
SELECT COUNT(*) AS ambiguous_move_groups
FROM (
    SELECT wt.invoice_account_move_id
    FROM woow_wallet_topup AS wt
    JOIN payment_transaction AS pt ON pt.id = wt.payment_transaction_id
    WHERE wt.invoice_account_move_id IS NOT NULL
      AND pt.reference IS NOT NULL
    GROUP BY wt.invoice_account_move_id
    HAVING COUNT(DISTINCT pt.reference) > 1
) AS ambiguous_moves;
