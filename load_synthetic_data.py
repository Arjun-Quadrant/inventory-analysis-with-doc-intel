import re
from openai import AzureOpenAI
from dotenv import load_dotenv
import psycopg2
import os
import random

load_dotenv()

# Azure OpenAI client
client = AzureOpenAI(
    api_version="2025-04-01-preview",
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_KEY"),
)

# PostgreSQL connection
conn = psycopg2.connect(os.getenv("POSTGRES_CONNECTION"))
cursor = conn.cursor()

def generate_descriptions():
    # Store chat history
    chat_history = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]

    all_fruits = set()
    while len(all_fruits) < 50:
        # Add a user message
        chat_history.append({"role": "user", "content": "Taking prior history into account, generate a comma separated list of 100 unique fruits that you have never mentioned in the past. Just return the list without any additional text and no dot at the end. Do not add add a period at the end but seperate every item with a comma."})
        # Get response from Azure OpenAI
        response = client.chat.completions.create(
            model=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"),
            messages=chat_history,
        )

        # Append the assistant's response to the chat history
        chat_history.append({"role": "assistant", "content": response.choices[0].message.content})

        fruits = response.choices[0].message.content.split(", ")
        for fruit in fruits:
            if fruit not in all_fruits:
                all_fruits.add(fruit.strip())
        
    all_fruits = sorted(list(all_fruits))

    fruit_to_descriptions = {}
    curr_batch = 1
    while len(fruit_to_descriptions) < 50:
        chat_history.append({"role": "user", "content": f"The goal is to generate a 3-4 sentence description for fruits between indexes {(curr_batch - 1) * 50} and {curr_batch * 50} in the list {all_fruits}. Return a JSON object that can be parsed and stay consistent with the format returned in the past. The JSON object should be an dictionary where each fruit is mapped to its description."})
        
        # Continue the conversation
        response = client.chat.completions.create(
            model=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"),
            messages=chat_history
        )

        response_llm = response.choices[0].message.content

        # Append the assistant's response to the chat history
        chat_history.append({"role": "assistant", "content": response_llm})

        pattern = r'"(.*?)"\s*:\s*"(.*?)"(?=,\n|,\s*}|$)'
        matches = re.findall(pattern, response_llm, re.DOTALL)
        # Convert to dictionary
        result = {key: value for key, value in matches}
        fruit_to_descriptions.update(result)
        curr_batch = curr_batch + 1
    return fruit_to_descriptions

def generate_unique_ids(n):
    prompt = f"Give me a list of {n} unique integers between 0 and 9999. Each integer must be exactly 4 digits (fill the leading digits with 0 if not). Return only a plain list without numbering or extra text."
    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant. Answer the user request with no extra text."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        max_tokens=4096,
        temperature=1.0,
        model=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
    )
    content = response.choices[0].message.content
    ids = [line.strip() for line in content.strip().split("\n") if line.strip()]
    return ids[:n]

def insert_synthetic_data(table_name):
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
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
    cursor.execute(f"ALTER TABLE bellevue ADD COLUMN IF NOT EXISTS spanish_description TEXT")
    conn.commit()
    name_to_description = generate_descriptions()
    ids = generate_unique_ids(len(name_to_description) * 2)
    count = 0
    for name, description in name_to_description.items():
        inventory_id = f"IN{ids[count]}"
        unit_price_num = random.randint(1, 20)
        unit_price = f"${unit_price_num}"
        quantity_in_stock = random.randint(10, 100)
        inventory_value = f"${unit_price_num * quantity_in_stock}"
        reorder_level = random.randint(5, 20)
        reorder_time_in_days = random.randint(1, 10)
        quantity_in_reorder = random.randint(0, 50)
        cursor.execute(f"""
            INSERT INTO {table_name} (
                inventory_id, name, description, unit_price, 
                quantity_in_stock, inventory_value, 
                reorder_level, reorder_time_in_days, quantity_in_reorder
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            inventory_id, name, description, unit_price,
            quantity_in_stock, inventory_value,
            reorder_level, reorder_time_in_days, quantity_in_reorder
        ))
        conn.commit()
        count += 1

    cursor.execute(f"""
                UPDATE {table_name}
                SET embedding = azure_openai.create_embeddings('{os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME")}', description, max_attempts => 5, retry_delay_ms => 500)
                WHERE embedding IS NULL;
                """)
    conn.commit()

    cursor.execute(f"""SELECT inventory_id, description FROM {table_name}""")
    info = cursor.fetchall()
    for item in info:
        inventory_id, desc = item
        cursor.execute(f"SELECT a.translations[1].text FROM azure_cognitive.translate(%s, 'es') a;", (desc,))
        spanish_description = cursor.fetchall()[0][0]

        cursor.execute(f"UPDATE {table_name} SET spanish_description = %s WHERE inventory_id = %s", (spanish_description, inventory_id))
        conn.commit()