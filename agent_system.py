import os
import json
import requests
from typing import TypedDict
from langgraph.graph import StateGraph, END

# 1. Настройки локального сервера LM Studio
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"

# 2. Динамическая структура нашего Блокнота (Стейт)
class AgentState(TypedDict):
    issue_description: str  # Описание бага из issue.txt
    original_code: str      # Исходный сломанный код из broken_code.py
    code_fix: str           # Сюда ИИ запишет исправленный вариант
    error_message: str      # Сюда валидатор или человек запишет ошибку
    attempts: int           # Счетчик попыток

# 3. Агент-Программист (работает через LM Studio)
def programmer_agent(state: AgentState):
    print(f"\n[Агент-Программист]: Анализирую баг и код. Попытка №{state['attempts'] + 1}")
    
    system_prompt = (
        "Ты — Senior Python разработчик. Твоя задача — исправить баг в предоставленном коде.\n"
        "Правила ответа:\n"
        "1. Выведи ТОЛЬКО готовый, исправленный, рабочий код Python целиком.\n"
        "2. Не пиши никаких объяснений, введений, заключений.\n"
        "3. Категорически запрещено использовать разметку markdown вроде ```python или ```. Только чистый текст кода."
    )
    
    user_prompt = (
        f"ОПИСАНИЕ БАГА:\n{state['issue_description']}\n\n"
        f"ИСХОДНЫЙ СЛОМАННЫЙ КОД:\n{state['original_code']}\n"
    )
    
    # Если это повторная попытка, передаем ИИ ошибку компиляции
    if state['error_message']:
        user_prompt += f"\nТвой предыдущий вариант кода выдал ОШИБКУ при запуске:\n{state['error_message']}\nИсправь её!"

    try:
        print("[Система]: Запрос отправлен в LM Studio...")
        response = requests.post(
            url=LM_STUDIO_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "model": "local-model", 
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0
            }),
            timeout=40
        )
        
        generated_code = response.json()["choices"][0]["message"]["content"].strip()
        
        # Защитная очистка, если модель всё же нарушила промпт и добавила markdown-кавычки
        if "```" in generated_code:
            lines = generated_code.split("\n")
            cleaned_lines = [line for line in lines if not line.strip().startswith("```")]
            generated_code = "\n".join(cleaned_lines).strip()
            
    except Exception as e:
        print(f"[Система]: Ошибка связи с LM Studio: {str(e)}")
        generated_code = state['original_code'] # В случае падения сети возвращаем исходный код

    return {
        "code_fix": generated_code,
        "attempts": state["attempts"] + 1
    }

# 4. Универсальный Механический валидатор (без хардкода функций!)
def validator_node(state: AgentState):
    print("\n[Валидатор]: Проверяю исправленный код на синтаксис и компиляцию...")
    code = state["code_fix"]
    
    if not code:
        return {"error_message": "Агент вернул пустой код"}
        
    try:
        # Пытаемся детерминированно скомпилировать полученный код встроенными средствами Python
        # Это проверяет синтаксис (SyntaxError, IndentationError и т.д.)
        compiled_code = compile(code, filename="<string>", mode="exec")
        
        # Пробуем выполнить его в изолированном пространстве имен
        # Это поймает очевидные runtime-ошибки вроде NameError или некорректного импорта
        local_scope = {}
        exec(compiled_code, {}, local_scope)
        
        print("[Валидатор]: Механическая проверка успешна! Синтаксических ошибок нет.")
        return {"error_message": ""}
    except Exception as e:
        error_info = f"{type(e).__name__}: {str(e)}"
        print(f"[Валидатор]: Внимание! Код сломался при проверке: {error_info}")
        return {"error_message": error_info}

# 4.5 Контроль человеком (Human-in-the-Loop)
def human_approval_node(state: AgentState):
    print("\n--- ТОЧКА КОНТРОЛЯ ЧЕЛОВЕКОМ (HITL) ---")
    print(f"Предлагаемый ИИ вариант кода:\n\n{state['code_fix']}\n")
    
    user_decision = input("Вы одобряете этот фикс? (yes/no): ").strip().lower()
    
    if user_decision == "yes":
        print("[Система]: Одобрено человеком. Сохраняю результат в файл...")
        # Перезаписываем наш broken_code.py уже исправленным кодом!
        with open("broken_code.py", "w", encoding="utf-8") as f:
            f.write(state["code_fix"])
        print("[Система]: Файл broken_code.py успешно обновлен!")
        return {"error_message": ""}
    else:
        print("[Система]: Отклонено человеком. Отправляем обратно.")
        return {"error_message": "Человек-ревьюер отклонил код. Перепроверь логику выполнения."}

# 5. Логика переходов графа
def route_after_validation(state: AgentState):
    if state["error_message"] == "":
        return "human_approval"
    elif state["attempts"] >= 3:
        print("\n[Система]: Превышено число попыток автоматического исправления.")
        return "end"
    else:
        print("[Система]: Возвращаем стейт агенту на доработку.")
        return "retry"

def route_after_human(state: AgentState):
    if "Человек-ревьюер" in state["error_message"]:
        return "retry"
    return "end"

# 6. Сборка графа LangGraph
workflow = StateGraph(AgentState)
workflow.add_node("programmer", programmer_agent)
workflow.add_node("validator", validator_node)
workflow.add_node("human_approval", human_approval_node)

workflow.set_entry_point("programmer")
workflow.add_edge("programmer", "validator")

workflow.add_conditional_edges(
    "validator", route_after_validation,
    {"human_approval": "human_approval", "end": END, "retry": "programmer"}
)
workflow.add_conditional_edges(
    "human_approval", route_after_human,
    {"end": END, "retry": "programmer"}
)

app = workflow.compile()

# === ШАГ НАЧАЛЬНОЙ ИНИЦИАЛИЗАЦИИ ИЗ ФАЙЛОВ ===
if __name__ == "__main__":
    print("[Система]: Читаю входные артефакты из файлов...")
    
    # Автоматически создаем файлы-заглушки, если их еще нет в папке, чтобы скрипт не падал
    if not os.path.exists("issue.txt"):
        with open("issue.txt", "w", encoding="utf-8") as f:
            f.write("Функция calculate_sum падает с ошибкой, если передать список строк вместо чисел.")
            
    if not os.path.exists("broken_code.py"):
        with open("broken_code.py", "w", encoding="utf-8") as f:
            f.write("def calculate_sum(items):\n    # Тут допущена ошибка типизации\n    return sum(items)\n")

    # Читаем данные из файлов "блокнотов"
    with open("issue.txt", "r", encoding="utf-8") as f:
        issue_data = f.read()
        
    with open("broken_code.py", "r", encoding="utf-8") as f:
        code_data = f.read()

    # Заполняем начальный State динамически!
    initial_input = {
        "issue_description": issue_data,
        "original_code": code_data,
        "code_fix": "",
        "error_message": "",
        "attempts": 0
    }

    # Запуск
    final_output = app.invoke(initial_input)
    print("\n--- Финальный стейт системы ---")
    print(f"Попыток сделано: {final_output['attempts']}")
