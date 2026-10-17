import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load environment variables
load_dotenv()

# Get API key
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY not found in .env file")

# Create Gemini client
client = genai.Client(api_key=api_key)

# QA Agent instructions
user_observation = input("\nDescribe the bug you found: ")

qa_prompt = f"""
You are an experienced QA engineer.

Analyze the following tester observation:

"{user_observation}"

Generate a professional bug report containing:

1. Bug Title
2. Module
3. Description
4. Steps to Reproduce
5. Expected Result
6. Actual Result
7. Severity
8. Priority
9. Possible Cause
10. Missing Information

Rules:
- Do not invent information.
- If information is unavailable, write "Not provided".
- Keep the report clear and professional.
- Steps to reproduce should be numbered.
- Severity and priority should include a short reason.
"""

# Send request to Gemini with automatic retry
response = None

for attempt in range(3):
    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=qa_prompt,
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                )
            )
        )
        break

    except Exception as e:
        print(f"\nAttempt {attempt + 1} failed: {e}")

        if attempt < 2:
            wait_time = 5 * (attempt + 1)
            print(f"Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
        else:
            print("\nGemini is currently unavailable. Please try again later.")
            raise

# Display result
print("\n--- QA AGENT RESPONSE ---\n")
print(response.text)