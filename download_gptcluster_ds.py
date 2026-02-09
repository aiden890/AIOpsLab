# Install dependencies as needed:
# pip install kagglehub[pandas-datasets]
import kagglehub
from kagglehub import KaggleDatasetAdapter

# # Set the path to the file you'd like to load
# file_path = "alibaba_cluster_dataset"

# # Load the latest version
# df = kagglehub.load_dataset(
#     KaggleDatasetAdapter.PANDAS,
#     "derrickmwiti/cluster-trace-gpu-v2020",
#     file_path,
#     # Provide any additional arguments like
#     # sql_query or pandas_kwargs. See the
#     # documenation for more information:
#     # https://github.com/Kaggle/kagglehub/blob/main/README.md#kaggledatasetadapterpandas
# )

# print("First 5 records:", df.head())

import kagglehub

# Download latest version
path = kagglehub.dataset_download("derrickmwiti/cluster-trace-gpu-v2020")

print("Path to dataset files:", path)