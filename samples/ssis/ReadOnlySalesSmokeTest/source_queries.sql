-- Preview_Source_Rows
SELECT SaleId, Region, Amount, IsActive
FROM (VALUES
    (1, 'East', CAST(10.00 AS DECIMAL(12,2)), 1),
    (2, 'East', CAST(20.50 AS DECIMAL(12,2)), 1),
    (3, 'West', CAST(7.25 AS DECIMAL(12,2)), 1),
    (4, 'West', CAST(99.00 AS DECIMAL(12,2)), 0),
    (5, 'East', CAST(NULL AS DECIMAL(12,2)), 1)
) AS sample(SaleId, Region, Amount, IsActive)
ORDER BY SaleId;

-- Aggregate_Active_Sales
SELECT Region, COUNT(*) AS ActiveRowCount, COUNT(Amount) AS NonNullAmountCount, SUM(Amount) AS TotalAmount
FROM (VALUES
    (1, 'East', CAST(10.00 AS DECIMAL(12,2)), 1),
    (2, 'East', CAST(20.50 AS DECIMAL(12,2)), 1),
    (3, 'West', CAST(7.25 AS DECIMAL(12,2)), 1),
    (4, 'West', CAST(99.00 AS DECIMAL(12,2)), 0),
    (5, 'East', CAST(NULL AS DECIMAL(12,2)), 1)
) AS sample(SaleId, Region, Amount, IsActive)
WHERE IsActive = 1
GROUP BY Region
ORDER BY Region;

-- Verify_Active_Totals
SELECT COUNT(*) AS ActiveRowCount, COUNT(Amount) AS NonNullAmountCount, SUM(Amount) AS TotalAmount
FROM (VALUES
    (1, 'East', CAST(10.00 AS DECIMAL(12,2)), 1),
    (2, 'East', CAST(20.50 AS DECIMAL(12,2)), 1),
    (3, 'West', CAST(7.25 AS DECIMAL(12,2)), 1),
    (4, 'West', CAST(99.00 AS DECIMAL(12,2)), 0),
    (5, 'East', CAST(NULL AS DECIMAL(12,2)), 1)
) AS sample(SaleId, Region, Amount, IsActive)
WHERE IsActive = 1;
