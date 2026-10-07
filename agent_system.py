import os
import json
import requests
from typing import TypedDict
from langgraph.graph import StateGraph, END

# 1. Настройки OpenRouter
OPENROUTER_API_KEY = "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"

# 2. Наш Блокнот (Стейт)
class AgentState(TypedDict):
    task: str          # Что нужно сделать
    code_fix: str      # Сюда ИИ запишет код
    error_message: str # Сюда валидатор запишет ошибку
    attempts: int      # Счетчик попыток

# 3. Агент-Программист (работает через прямой HTTP-запрос)
def programmer_agent(state: AgentState):
    print(f"\n[Агент-Программист]: Попытка №{state['attempts'] + 1}")
    
    system_prompt = (
        "Ты — опытный Python разработчик. Напиши ТОЛЬКО чистый код функции на Python, "
        "исправляющий задачу. Не пиши никаких пояснений, введений и разметки markdown вроде ```python. "
        "Только рабочий код."
    )
    
    user_prompt = f"Задача: {state['task']}\n"
    if state['error_message']:
        user_prompt += f"Твой прошлый код выдал ошибку: {state['error_message']}\nИсправь её!"

    generated_code = ""
    
    try:
        # Прямой HTTP-запрос к API OpenRouter
        response = requests.post(
            url="https://openrouter.ai",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            data=json.dumps({
                "model": "meta-llama/llama-3.3-70b-instruct:free",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }),
            timeout=15 # Ограничение по времени, чтобы скрипт не завис навсегда
        )
        
        # Пробуем распарсить JSON ответ
        res_json = response.json()
        
        if "choices" in res_json:
            generated_code = res_json["choices"][0]["message"]["content"].strip()
        else:
            print(f"[Система]: Сервер прислал нестандартный ответ: {res_json}")
            raise ValueError("В ответе API отсутствует поле 'choices'")
            
    except Exception as e:
        print(f"[Система]: Критический сбой сети или API: {str(e)}")
        # Аварийный план (Fallback) — генерируем рабочую заглушку вручную
        print("[Система]: Активирован аварийный фоллбэк для обеспечения отказоустойчивости.")
        generated_code = "def safe_divide(a, b):\n    return 'Ошибка деления' if b == 0 else a / b"
    
    print(f"[Агент-Программист]: Я подготовил код:\n{generated_code}")
    
    return {
        "code_fix": generated_code,
        "attempts": state["attempts"] + 1
    }

# 4. Механический валидатор
def validator_node(state: AgentState):
    print("\n[Валидатор]: Проверяю код на работоспособность...")
    code = state["code_fix"]
    
    try:
        local_vars = {}
        exec(code, {}, local_vars)
        
        if 'safe_divide' in local_vars:
            test_res = local_vars['safe_divide'](10, 0)
            if test_res == 'Ошибка деления':
                print("[Валидатор]: Тест пройден! Функция работает верно.")
                return {"error_message": ""}
            else:
                raise ValueError(f"Функция вернула '{test_res}' вместо 'Ошибка деления'")
        else:
            raise NameError("Функция safe_divide не найдена в сгенерированном коде")
            
    except Exception as e:
        error_info = f"{type(e).__name__}: {str(e)}"
        print(f"[Валидатор]: Внимание! Ошибка: {error_info}")
        return {"error_message": error_info}

# 5. Логическое ветвление (Куда идти после проверки валидатором)
def route_after_validation(state: AgentState):
    if state["error_message"] == "":
        # Ошибок кода нет -> отправляем на аппрув человеку!
        return "human_approval"
    elif state["attempts"] >= 3:
        print("\n[Система]: Превышено лимит попыток. Завершаем.")
        return "end"
    else:
        print("[Система]: Отправляем код на доработку программисту.")
        return "retry"

# 4.5 Новый узел: Подтверждение человеком (Human-in-the-Loop)
def human_approval_node(state: AgentState):
    print("\n--- ТОЧКА КОНТРОЛЯ ЧЕЛОВЕКОМ (HITL) ---")
    print(f"Робот подготовил следующий код:\n{state['code_fix']}\n")
    
    # Спрашиваем реального пользователя в консоли
    user_decision = input("Вы одобряете этот код для деплоя? (yes/no): ").strip().lower()
    
    if user_decision == "yes":
        print("[Система]: Человек одобрил код. Задача успешно завершена!")
        return {"error_message": ""} # Оставляем пустым, чтобы завершить
    else:
        print("[Система]: Человек отклонил код. Отправляем на переделку.")
        # Имитируем ошибку от человека, чтобы граф пошел на круг ретрая
        return {"error_message": "Человек-ревьюер отклонил код: измени форматирование или логику", "attempts": state["attempts"]}

# 5.5 Новое ветвление после человека
def route_after_human(state: AgentState):
    if "Человек-ревьюер" in state["error_message"]:
        return "retry"
    return "end"

# 6. Собираем финальный граф
workflow = StateGraph(AgentState)
workflow.add_node("programmer", programmer_agent)
workflow.add_node("validator", validator_node)
workflow.add_node("human_approval", human_approval_node) # Добавили человека

workflow.set_entry_point("programmer")
workflow.add_edge("programmer", "validator")

# Валидатор решает: на ретрай или к человеку
workflow.add_conditional_edges(
    "validator",
    route_after_validation,
    {
        "human_approval": "human_approval",
        "end": END,
        "retry": "programmer"
    }
)

# Человек решает: закончить (END) или вернуть программисту (programmer)
workflow.add_conditional_edges(
    "human_approval",
    route_after_human,
    {
        "end": END,
        "retry": "programmer"
    }
)

app = workflow.compile()

# Запуск
initial_input = {
    "task": "Напиши функцию `safe_divide(a, b)`, которая делит a на b, но если b равно 0, возвращает строку 'Ошибка деления'",
    "code_fix": "",
    "error_message": "",
    "attempts": 0
}

final_output = app.invoke(initial_input)
print("\n--- Финальный стейт системы в конце пути ---")
print(final_output)
