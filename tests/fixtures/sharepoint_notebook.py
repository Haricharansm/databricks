# Databricks notebook source
# Migration review gate: engine output is experimental and has not been executed.
dbutils.widgets.dropdown("migration_reviewed", "false", ["false", "true"])
if dbutils.widgets.get("migration_reviewed") != "true":
    raise RuntimeError("Review generated code, SSIS findings, connections, parameters, and workflow paths before setting migration_reviewed=true.")

# COMMAND ----------
from curses import meta
from logging import exception
import sys
import re
import socket
import getpass
import uuid
import pandas as pd
from datetime import datetime, timedelta
from pyspark.context import SparkContext
from pyspark.sql.functions import *

StartTime = datetime.now().strftime("%Y%m%d %H:%M:%S")
MachineName = socket.gethostname()
UserName = getpass.getuser()
ExecutionInstanceGUID = str(uuid.uuid4())
PackageName = 'Package'





# COMMAND ----------

# Processing node Package\Data Flow Task\REST Source, type MANAGEDCOMPONENTHOST
# component nameREST Source
#We do not support conversion of this component.

%DF% = f"""SELECT * FROM """
Package_Data_Flow_Task_REST_Source = spark.sql(%DF%)
Package_Data_Flow_Task_REST_Source.createOrReplaceTempView('Package_Data_Flow_Task_REST_Source')
