import os
import psycopg2
from flask import Flask, request, render_template, redirect
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

# Load env variables
load_dotenv()

# Azure Form Recognizer client
form_client = DocumentAnalysisClient(
    endpoint=os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT"),
    credential=AzureKeyCredential(os.getenv("AZURE_FORM_RECOGNIZER_KEY"))
)

# PostgreSQL connection
conn = psycopg2.connect(os.getenv("POSTGRES_CONNECTION"))
cursor = conn.cursor()

# Flask app
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def upload_pdf():
    if request.method == "POST":
        file = request.files["pdf_file"]
        if file.filename.endswith(".pdf"):
            pdf_bytes = file.read()
            poller = form_client.begin_analyze_document("prebuilt-document", document=pdf_bytes)
            result = poller.result()
            table_name = result.paragraphs[0].content.replace(" ", "_")
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.commit()
            # Create table if not exists
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
                quantity_in_reorder INTEGER
            )""")
            conn.commit()

            # I can recreate the table structure with a 2D array
            for table in result.tables:
                matrix = [[0 for _ in range(table.column_count)] for _ in range(table.row_count)]
                for cell in table.cells:
                    matrix[cell.row_index][cell.column_index] = cell.content

                # Skip the first row (header)
                for row_index in range(1, len(matrix)):
                    row = matrix[row_index]
                    cursor.execute(f"""INSERT INTO {table_name}
                                        (inventory_id, name, description, unit_price, quantity_in_stock, inventory_value, reorder_level, reorder_time_in_days, quantity_in_reorder) 
                                        VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                        (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]))
            conn.commit()
            return render_template("success.html")
        else:
            return render_template("failure.html")
    return render_template("upload.html")

@app.route("/upload", methods=["POST"])
def back_to_upload():
    return render_template("upload.html")

if __name__ == "__main__":
    app.run(debug=True)