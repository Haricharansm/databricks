CREATE PROCEDURE dbo.RefreshCustomers AS
BEGIN
    SET NOCOUNT ON;
    UPDATE c SET Region = 'Unknown' FROM dbo.Customers AS c WHERE c.Region IS NULL;
    SELECT * FROM dbo.CustomerTotals;
END;
