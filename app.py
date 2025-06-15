import os
import psycopg2

from flask import Flask, request, render_template
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceExistsError
from openai import AzureOpenAI
from load_synthetic_data import insert_synthetic_data
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Azure Document Intelligence client
form_client = DocumentAnalysisClient(
    endpoint=os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT"),
    credential=AzureKeyCredential(os.getenv("AZURE_FORM_RECOGNIZER_KEY"))
)

# Azure Blob Storage client
blob_service_client = BlobServiceClient.from_connection_string(os.getenv("AZURE_BLOB_CONNECTION_STRING"))

# PostgreSQL connection
conn = psycopg2.connect(os.getenv("POSTGRES_CONNECTION"))
cursor = conn.cursor()

# Azure OpenAI client
client = AzureOpenAI(
    api_version="2024-12-01-preview",
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_KEY"),
)

# Flask app
app = Flask(__name__)

def get_all_tables():
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    table_names = [row[0] for row in cursor.fetchall()]
    return table_names

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

                # Analyze using Form Recognizer (this can take a while, around 30 seconds per document)
                poller = form_client.begin_analyze_document("prebuilt-document", document=pdf_bytes)
                result = poller.result()
                table_name = result.paragraphs[0].content.replace(" ", "_")
                cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                conn.commit()
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cursor.execute("CREATE EXTENSION IF NOT EXISTS azure_ai;")
                conn.commit()
                cursor.execute(f"SELECT azure_ai.set_setting('azure_openai.endpoint', '{os.getenv('AZURE_OPENAI_ENDPOINT')}');")
                cursor.execute(f"SELECT azure_ai.set_setting('azure_openai.subscription_key', '{os.getenv('AZURE_OPENAI_KEY')}');")
                cursor.execute(f"SELECT azure_ai.set_setting('azure_cognitive.endpoint', '{os.getenv('AZURE_COGNITIVE_ENDPOINT')}');")
                cursor.execute(f"SELECT azure_ai.set_setting('azure_cognitive.subscription_key', '{os.getenv('AZURE_COGNITIVE_KEY')}');")
                cursor.execute(f"SELECT azure_ai.set_setting('azure_cognitive.region', '{os.getenv('AZURE_COGNITIVE_REGION')}');")
                conn.commit()
                cursor.execute(f"""
                CREATE TABLE {table_name} (
                    inventory_id TEXT UNIQUE,
                    name TEXT UNIQUE,
                    description TEXT,
                    unit_price TEXT,
                    quantity_in_stock INTEGER,
                    inventory_value TEXT,
                    reorder_level INTEGER,
                    reorder_time_in_days INTEGER,
                    quantity_in_reorder INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    embedding VECTOR(1536),
                    PRIMARY KEY (inventory_id)
                )""")
                conn.commit()
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS spanish_description TEXT")
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
                        cursor.execute(f"""UPDATE {table_name}
                                            SET embedding = azure_openai.create_embeddings('{os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME")}', description, max_attempts => 5, retry_delay_ms => 500)
                                            WHERE embedding IS NULL""")              
                        # Go through each row of the table, get the description for the row, translate it into Spanish, format it, and update the table
                        english_description = row[2]
                        cursor.execute(f"SELECT a.translations[1].text FROM azure_cognitive.translate('{english_description}', 'es') a;")
                        spanish_description = cursor.fetchall()[0][0]
                        cursor.execute(f"UPDATE {table_name} SET spanish_description = '{spanish_description}' WHERE description = '{english_description}'")
                conn.commit()

            else:
                return render_template("failure.html")
        return render_template("success.html")
    table_names = get_all_tables()
    return render_template("upload.html", table_names=table_names)

@app.route("/upload", methods=["POST"])
def back_to_upload():
    table_names = get_all_tables()
    return render_template("upload.html", table_names=table_names)

@app.route("/ask-question", methods=["POST"])
def ask_question():
    question = request.form.get("question")
    selected_table = request.form.get("table_name")
    cursor.execute(f"""
        SELECT name FROM {selected_table}
        ORDER BY embedding <=> azure_openai.create_embeddings('{os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME")}', '{question}')::vector
        LIMIT 10;
    """)
    results = cursor.fetchall()
    results = [result[0] for result in results]
    return render_template("answer.html", table=selected_table, answer_list=results)


@app.route("/load-synthetic", methods=["POST"])
def load_synthetic_data():
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")

    table_names = [row[0] for row in cursor.fetchall()]
    for table_name in table_names:
        # Insert synthetic data into the tables
        insert_synthetic_data(table_name)
    return render_template("success.html")

@app.route("/check-tables", methods=["GET"])
def check_tables():
    cursor.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'")
    count = cursor.fetchone()[0]
    return {"has_tables": count > 0}


if __name__ == "__main__":
    app.run(debug=True)