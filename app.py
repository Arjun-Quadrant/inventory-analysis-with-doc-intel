import os
import psycopg2
from flask import Flask, request, render_template
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceExistsError

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Azure clients
form_client = DocumentAnalysisClient(
    endpoint=os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT"),
    credential=AzureKeyCredential(os.getenv("AZURE_FORM_RECOGNIZER_KEY"))
)

blob_service_client = BlobServiceClient.from_connection_string(os.getenv("AZURE_BLOB_CONNECTION_STRING"))

# PostgreSQL connection
conn = psycopg2.connect(os.getenv("POSTGRES_CONNECTION"))
cursor = conn.cursor()

# Flask app
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def upload_pdf():
    if request.method == "POST":
        files = request.files.getlist("pdf_file")
        for file in files:
            if file and file.filename.endswith(".pdf"):
                pdf_bytes = file.read()

                # Upload to blob storage in a new container
                container_name = "inventory-forms"
                try:
                    container_client = blob_service_client.create_container(container_name)
                except ResourceExistsError:
                    container_client = blob_service_client.get_container_client(container_name)

                blob_name = file.filename                
                blob_client = container_client.get_blob_client(blob_name)
                blob_client.upload_blob(pdf_bytes, overwrite=True)

                # Analyze using Form Recognizer
                poller = form_client.begin_analyze_document("prebuilt-document", document=pdf_bytes)
                result = poller.result()

                table_name = result.paragraphs[0].content.replace(" ", "_")
                cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                conn.commit()

                cursor.execute(f"""
                CREATE TABLE {table_name} (
                    inventory_id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    unit_price TEXT,
                    quantity_in_stock INTEGER,
                    inventory_value TEXT,
                    reorder_level INTEGER,
                    reorder_time_in_days INTEGER,
                    quantity_in_reorder INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""")
                conn.commit()

                for table in result.tables:
                    matrix = [[0 for _ in range(table.column_count)] for _ in range(table.row_count)]
                    for cell in table.cells:
                        matrix[cell.row_index][cell.column_index] = cell.content

                    for row_index in range(1, len(matrix)):
                        row = matrix[row_index]
                        cursor.execute(f"""INSERT INTO {table_name}
                                            (inventory_id, name, description, unit_price, quantity_in_stock, inventory_value, reorder_level, reorder_time_in_days, quantity_in_reorder) 
                                            VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                            (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]))
                conn.commit()
            else:
                return render_template("failure.html")
        return render_template("success.html")
    return render_template("upload.html")

@app.route("/upload", methods=["POST"])
def back_to_upload():
    return render_template("upload.html")

if __name__ == "__main__":
    app.run(debug=True)