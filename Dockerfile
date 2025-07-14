FROM python:3.11-slim

WORKDIR /app

# Скопіювати файли в контейнер
COPY . .

# Встановити бібліотеки
RUN pip install --no-cache-dir -r requirements.txt

# Запуск бота і Streamlit одночасно
CMD python3 bot.py & streamlit run testv2.py --server.port=$PORT --server.address=0.0.0.0
