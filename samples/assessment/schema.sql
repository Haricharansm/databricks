CREATE TABLE dbo.Customers (CustomerId INT, Region VARCHAR(30), Amount DECIMAL(12,2));
CREATE VIEW dbo.CustomerTotals AS SELECT Region, SUM(Amount) AS TotalAmount FROM dbo.Customers GROUP BY Region;
