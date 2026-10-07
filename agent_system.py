import os
import json
import requests
from typing import TypedDict
from langgraph.graph import StateGraph, END

# 1. Настройки основного API (OpenRouter)
OPENROUTER_API_KEY = "sk-or-твоя-строка-ключа"

# 2. Структура нашего Блокнота (Стейт)
class AgentState(TypedDict):
    task: str          # Что нужно сделать
    code_fix: str      # Сюда ИИ запишет код
    error_message: str # Сюда валидатор или человек запишет ошибку
    attempts: int      # Счетчик попыток

# 3. Агент-Программист с автоматическим переключением на резервный ИИ при сбое
def programmer_agent(state: AgentState):
    print(f"\n[Агент-Программист]: Попытка №{state['attempts'] + 1}")
    
    system_prompt = (
        "Ты — опытный Python разработчик. Напиши ТОЛЬКО чистый код функции на Python, "
        "исправляющий задачу. Не пиши никаких пояснений, введений и разметки markdown вроде ```python. "
        "Только рабочий код."
    )
    
    user_prompt = f"Задача: {state['task']}\n"
    if state['error_message']:
        user_prompt += f"Предыдущая ошибка/замечание: {state['error_message']}\nИсправь это в коде!"

    generated_code = ""
    
    try:
        print("[Система]: Пробую отправить запрос на основной сервер (OpenRouter)...")
        # Основной запрос к OpenRouter
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
            timeout=10
        )
        # Если здесь вылетит ошибка JSON (line 1 column 1), код уйдет в блок except
        res_json = response.json()
        generated_code = res_json["choices"][0]["message"]["content"].strip()
        print("[Агент-Программист]: Успешно получил ответ от OpenRouter.")
        
    except Exception as e:
        print(f"[Система]: Основной сервер (OpenRouter) вернул ошибку: {str(e)}")
        print("[Система]: АКТИВИРУЮ РЕЗЕРВНЫЙ ИИ (Отказоустойчивость / Circuit Breaker pattern)...")
        
        try:
            # Резервный полностью бесплатный и стабильный шлюз Pollinations AI (работает без ключей на базе Llama 3)
            fallback_url = "https://pollinations.ai"
            payload = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "model": "llama",
                "jsonMode": False
            }
            fb_response = requests.post(fallback_url, json=payload, timeout=15)
            generated_code = fb_response.text.strip()
            
            # На всякий случай очищаем от разметки markdown, если модель её добавила
            if "```python" in generated_code:
                generated_code = generated_code.split("```python")[1].split("```")[0].strip()
            elif "```" in generated_code:
                generated_code = generated_code.split("```")[1].split("```")[0].strip()
                
            print("[Агент-Программист]: Успешно получил ответ от Резервного ИИ.")
        except Exception as fb_err:
            print(f"[Система]: Полный отказ всех нейросетей: {str(fb_err)}")
            # Самый крайний случай — жесткий фоллбэк на базовый шаблон
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
                print("[Валидатор]: Механический тест пройден! Функция работает верно.")
                return {"error_message": ""}
            else:
                raise ValueError(f"Функция вернула '{test_res}' вместо 'Ошибка деления'")
        else:
            raise NameError("Функция safe_divide не найдена в сгенерированном коде")
            
    except Exception as e:
        error_info = f"{type(e).__name__}: {str(e)}"
        print(f"[Валидатор]: Внимание! Обнаружена ошибка: {error_info}")
        return {"error_message": error_info}

# 4.5 Контроль человеком (Human-in-the-Loop)
def human_approval_node(state: AgentState):
    print("\n--- ТОЧКА КОНТРОЛЯ ЧЕЛОВЕКОМ (HITL) ---")
    print(f"Сгенерированный код выглядит так:\n\n{state['code_fix']}\n")
    
    user_decision = input("Вы одобряете этот код для деплоя? (yes/no): ").strip().lower()
    
    if user_decision == "yes":
        print("[Система]: Человек одобрил код. Задача успешно завершена!")
        return {"error_message": ""}
    else:
        print("[Система]: Человек отклонил код. Отправляем на доработку.")
        return {"error_message": "Человек-ревьюер отклонил код: перепиши логику аккуратнее"}

# 5. Логическое ветвление
def route_after_validation(state: AgentState):
    if state["error_message"] == "":
        return "human_approval"
    elif state["attempts"] >= 3:
        print("\n[Система]: Превышено лимит попыток. Завершаем.")
        return "end"
    else:
        print("[Система]: Отправляем код обратно на доработку программисту.")
        return "retry"

def route_after_human(state: AgentState):
    if "Человек-ревьюер" in state["error_message"]:
        return "retry"
    return "end"

# 6. Сборка графа
workflow = StateGraph(AgentState)
workflow.add_node("programmer", programmer_agent)
workflow.add_node("validator", validator_node)
workflow.add_node("human_approval", human_approval_node)

workflow.set_entry_point("programmer")
workflow.add_edge("programmer", "validator")

workflow.add_conditional_edges(
    "validator",
    route_after_validation,
    {
        "human_approval": "human_approval",
        "end": END,
        "retry": "programmer"
    }
)

workflow.add_conditional_edges(
    "human_approval",
    route_after_human,
    {
        "end": END,
        "retry": "programmer"
    }
)

app = workflow.compile()

# Запуск системы
initial_input = {
    "task": "Напиши функцию `safe_divide(a, b)`, которая делит a на b, но если b равно 0, возвращает строку 'Ошибка деления'",
    "code_fix": "",
    "error_message": "",
    "attempts": 0
}

final_output = app.invoke(initial_input)
print("\n--- Финальный стейт системы в конце пути ---")
print(final_output)
