from flask import Flask, render_template, request, jsonify
from openai import OpenAI
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(verify=False),
)

from soul import SOUL

GREETING_PROMPT = "[SYSTÈME: C'est le tout premier message de la conversation. Envoie un message d'accroche court et naturel — comme si tu venais de matcher ou d'être mise en contact avec cette personne. Maximum 1-2 phrases. Naturel, pas trop enthousiaste.]"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    user_message = data.get("message", "").strip()
    history = data.get("history", [])
    is_greeting = data.get("greeting", False)

    if not user_message and not is_greeting:
        return jsonify({"error": "Empty message"}), 400

    if is_greeting:
        messages = [
            {"role": "system", "content": SOUL},
            {"role": "user", "content": GREETING_PROMPT},
        ]
    else:
        messages = [{"role": "system", "content": SOUL}] + history + [
            {"role": "user", "content": user_message}
        ]

    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=messages,
    )

    assistant_message = response.choices[0].message.content

    if is_greeting:
        updated_history = [{"role": "assistant", "content": assistant_message}]
    else:
        updated_history = history + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]

    return jsonify({"response": assistant_message, "history": updated_history})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
