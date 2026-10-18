USE [RehabPatients-ylh]
GO
/****** 物件:  Table [dbo].[EMGData]    指令碼日期: 2026/10/18 下午 02:50:51 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[EMGData](
	[EMGDataID] [nvarchar](50) NOT NULL,
	[TestID] [nvarchar](50) NOT NULL,
	[TimeOffsetSec] [smallint] NULL,
	[MuscleName] [nvarchar](100) NULL,
	[AvgRMSmV] [float] NULL,
	[MaxRMSmV] [float] NULL,
	[BaseRMSmV] [float] NULL,
	[DeviationPct] [decimal](10, 2) NULL,
	[AlertStatus] [varchar](20) NULL,
	[QualityStatus] [nvarchar](20) NULL,
	[RecordedAt] [datetime] NULL,
 CONSTRAINT [PK_EMGData] PRIMARY KEY CLUSTERED 
(
	[EMGDataID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** 物件:  Table [dbo].[PedalData]    指令碼日期: 2026/10/18 下午 02:50:51 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[PedalData](
	[PedalDataID] [nvarchar](50) NOT NULL,
	[TestID] [nvarchar](50) NOT NULL,
	[TimeOffsetSec] [smallint] NULL,
	[TotalPowerW] [float] NULL,
	[LeftPowerW] [float] NULL,
	[RightPowerW] [float] NULL,
	[LeftBalancePct] [decimal](10, 2) NULL,
	[RightBalancePct] [decimal](10, 2) NULL,
	[CadenceRPM] [float] NULL,
	[Status] [nvarchar](50) NULL,
	[RecordedAt] [datetime] NULL,
 CONSTRAINT [PK_PedalData] PRIMARY KEY CLUSTERED 
(
	[PedalDataID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** 物件:  Table [dbo].[Sessions]    指令碼日期: 2026/10/18 下午 02:50:51 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Sessions](
	[TestID] [nvarchar](50) NOT NULL,
	[PatientID] [nvarchar](50) NOT NULL,
	[Name] [nvarchar](100) NULL,
	[Sex] [nvarchar](10) NULL,
	[ActionType] [nvarchar](50) NULL,
	[TestTime] [int] NULL,
	[ResistanceLevel] [decimal](10, 2) NULL,
	[HasBaseline] [bit] NULL,
	[Status] [nvarchar](20) NULL,
	[AvgPowerW] [float] NULL,
	[MaxPowerW] [float] NULL,
	[AvgCadenceRPM] [float] NULL,
	[TotalDistanceKm] [float] NULL,
	[AvgLeftBalancePct] [decimal](10, 2) NULL,
	[AvgRightBalancePct] [decimal](10, 2) NULL,
	[Notes] [nvarchar](1000) NULL,
	[CreatedAt] [datetime] NULL,
 CONSTRAINT [PK_Sessions] PRIMARY KEY CLUSTERED 
(
	[TestID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** 物件:  Table [dbo].[TestBaselines]    指令碼日期: 2026/10/18 下午 02:50:51 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[TestBaselines](
	[BaseID] [nvarchar](50) NOT NULL,
	[TestID] [nvarchar](50) NOT NULL,
	[MuscleName] [nvarchar](100) NULL,
	[BaseTime] [smallint] NULL,
	[ActionType] [nvarchar](50) NULL,
	[BaseRMSmV] [float] NULL,
	[YellowDeviationPct] [decimal](8, 2) NULL,
	[RedDeviationPct] [decimal](8, 2) NULL,
	[QualityStatus] [varchar](20) NULL,
	[CreatedAt] [datetime] NULL,
 CONSTRAINT [PK_TestBaselines] PRIMARY KEY CLUSTERED 
(
	[BaseID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** 物件:  Table [dbo].[TrainerData]    指令碼日期: 2026/10/18 下午 02:50:51 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[TrainerData](
	[TrainerDataID] [nvarchar](50) NOT NULL,
	[TestID] [nvarchar](50) NOT NULL,
	[TimeOffsetSec] [smallint] NULL,
	[DistanceKm] [float] NULL,
	[SpeedKmh] [float] NULL,
	[PowerW] [float] NULL,
	[CadenceRPM] [float] NULL,
	[ResistanceLevel] [decimal](10, 2) NULL,
	[QualityStatus] [nvarchar](20) NULL,
	[RecordedAt] [datetime] NULL,
 CONSTRAINT [PK_TrainerData] PRIMARY KEY CLUSTERED 
(
	[TrainerDataID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[EMGData] ADD  CONSTRAINT [DF_EMGData_EMGDataID]  DEFAULT ('EMG-'+CONVERT([varchar](36),newid())) FOR [EMGDataID]
GO
ALTER TABLE [dbo].[EMGData] ADD  CONSTRAINT [DF_EMGData_RecordedAt]  DEFAULT (getdate()) FOR [RecordedAt]
GO
ALTER TABLE [dbo].[PedalData] ADD  CONSTRAINT [DF_PedalData_PedalDataID]  DEFAULT ('PEDAL-'+CONVERT([varchar](36),newid())) FOR [PedalDataID]
GO
ALTER TABLE [dbo].[PedalData] ADD  CONSTRAINT [DF_PedalData_RecordedAt]  DEFAULT (getdate()) FOR [RecordedAt]
GO
ALTER TABLE [dbo].[Sessions] ADD  CONSTRAINT [DF_Sessions_TestID]  DEFAULT ('TEST-'+CONVERT([varchar](36),newid())) FOR [TestID]
GO
ALTER TABLE [dbo].[Sessions] ADD  CONSTRAINT [DF_TestSessions_CreatedAt]  DEFAULT (getdate()) FOR [CreatedAt]
GO
ALTER TABLE [dbo].[TestBaselines] ADD  CONSTRAINT [DF_TestBaselines_BaseID]  DEFAULT ('BASE-'+CONVERT([varchar](36),newid())) FOR [BaseID]
GO
ALTER TABLE [dbo].[TestBaselines] ADD  CONSTRAINT [DF_TestBaselines_CreatedAt]  DEFAULT (getdate()) FOR [CreatedAt]
GO
ALTER TABLE [dbo].[TrainerData] ADD  CONSTRAINT [DF_TrainerData_TrainerDataID]  DEFAULT ('TRAINER-'+CONVERT([varchar](36),newid())) FOR [TrainerDataID]
GO
ALTER TABLE [dbo].[TrainerData] ADD  CONSTRAINT [DF_TrainerData_RecordedAt]  DEFAULT (getdate()) FOR [RecordedAt]
GO
