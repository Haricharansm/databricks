CREATE TABLE [dbo].[deFSite](
	[SiteID] [int] IDENTITY(1,1) NOT NULL,
--	[MenuID] [int] NULL,
	[SiteCode] [nvarchar](100) NULL,
	[SiteLocation] [nvarchar](500) NULL,
	[SiteName] [nvarchar](255) NULL,
	[SiteDesc] [nvarchar](255) NULL,
	[IsEnabled] [bit] NULL,
	[IsDeleted] [bit] NULL,
	[CreatedOn] [datetime] NULL,
	[CreatedBy] [int] NULL,
	[ModifiedOn] [datetime] NULL,
	[ModifiedBy] [int] NULL,
PRIMARY KEY CLUSTERED 
(
	[SiteID] ASC
)WITH (STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]

CREATE TABLE [dbo].[defRoleType](
	[RoleTypeID] [int] IDENTITY(1,1) NOT NULL,
	[RoleType] [nvarchar](255) NULL,
	[RoleTypeDesc] [nvarchar](255) NULL,
	[IsEnabled] [bit] NULL,
	[IsDeleted] [bit] NULL,
	[CreatedOn] [datetime] NULL,
	[CreatedBy] [int] NULL,
	[ModifiedOn] [datetime] NULL,
	[ModifiedBy] [int] NULL
PRIMARY KEY CLUSTERED 
(
	[Pk_RoleTypeID] ASC
)WITH (STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO

CREATE TABLE [dbo].[defRole](
	[RoleID] [int] IDENTITY(1,1) NOT NULL,
	[RoleName] [nvarchar](255) NULL,
	[RoleDesc] [nvarchar](255) NULL,
	[RoleLevel] [int] NULL,
	[RoleTypeID] [int] NOT NULL,
	[SiteID] [int] NULL,
	[IsEnabled] [bit] NULL,
	[IsDeleted] [bit] NULL,
	[CreatedOn] [datetime] NULL,
	[CreatedBy] [int] NULL,
	[ModifiedOn] [datetime] NULL,
	[ModifiedBy] [int] NULL
 CONSTRAINT [PK_defRole] PRIMARY KEY CLUSTERED 
(
	[RoleID] ASC
)WITH (STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO


CREATE TABLE [dbo].[defUser](
	[UserID] [int] IDENTITY(1,1) NOT NULL,
	--[SiteID] [int] NULL,
	[UserName] [nvarchar](255) NULL,
	[UserEmail] [nvarchar](255) NULL,
	[PasswordHash] [varbinary](16) NULL,
	[IsEnabled] [bit] NULL,
	[IsDeleted] [bit] NULL,
	[CreatedOn] [datetime] NULL,
	[CreatedBy] [int] NULL,
	[ModifiedOn] [datetime] NULL,
	[ModifiedBy] [int] NULL,
PRIMARY KEY CLUSTERED 
(
	[UserID] ASC
)WITH (STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO


CREATE TABLE [dbo].[defUserSiteMapping](
	[SiteMappingID] [int] IDENTITY(1,1) NOT NULL,
	[UserID] [int] NULL,
	[SiteID] [int] NULL,
	[IsEnabled] [bit] NULL,
	[IsDeleted] [bit] NULL,
	[CreatedOn] [datetime] NULL,
	[CreatedBy] [int] NULL,
	[ModifiedOn] [datetime] NULL,
	[ModifiedBy] [int] NULL,
PRIMARY KEY CLUSTERED 
(
	[SiteMappingID] ASC
)WITH (STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO

CREATE TABLE [dbo].[defUsersRoleMapping](
	[RoleMappingID] [int] IDENTITY(1,1) NOT NULL,
	[SiteMappingID] [int] NULL,
	[RoleID] [int] NULL,
	[IsEnabled] [bit] NULL,
	[IsDeleted] [bit] NULL,
	[CreatedOn] [datetime] NULL,
	[CreatedBy] [int] NULL,
	[ModifiedOn] [datetime] NULL,
	[ModifiedBy] [int] NULL,
PRIMARY KEY CLUSTERED 
(
	[RoleMappingID] ASC
)WITH (STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO