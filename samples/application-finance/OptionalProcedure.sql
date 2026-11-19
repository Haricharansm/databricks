-- Optional assessment asset. Calls require explicit reviewed orchestration.
CREATE PROCEDURE dbo.usp_CrosslakeDailyLedgerTotals @BusinessDate DATE
AS
BEGIN
    SELECT BusinessDate, COUNT(*) AS LedgerCnt, SUM(Amount) AS TotalAmount
    FROM dbo.CrosslakeDemoLedger
    WHERE BusinessDate = @BusinessDate
    GROUP BY BusinessDate;
END;
