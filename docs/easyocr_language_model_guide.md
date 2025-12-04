### **Core Principle: Mutually Exclusive Models**

The most important rule in EasyOCR is that models for different primary scripts (such as Cyrillic, Japanese, Arabic, etc.) are **mutually exclusive**. You can only use **one** of these non-Latin models at a time.

Attempting to mix languages from different primary groups (e.g., recognizing Russian and Japanese simultaneously) is **not supported** and will cause the application to crash.

### **How Language Selection Works: The "Dominant Group" Rule**

1.  **Latin Group:** This is the default group. It includes English, French, German, Spanish, Romanian, etc. You can freely select and combine **any** languages from this group as long as no "dominant" group is active.
    *   *Example:* `['en', 'fr', 'de']` is a valid combination.

2.  **"Dominant" Groups:** These are groups with non-Latin scripts. This includes:
    *   Cyrillic (Russian, Ukrainian, etc.)
    *   Japanese
    *   Korean
    *   Chinese
    *   Arabic
    *   And others...

3.  **Activating a Dominant Group:** As soon as you select **any** language from a dominant group (e.g., "Russian"), that group becomes active.

### **Rules for an Active Dominant Group**

When a dominant group is active (e.g., Cyrillic):

1.  **Only One:** You **cannot** select languages from other dominant groups. If Cyrillic is active, then Japanese, Arabic, and others become unavailable.
2.  **English – The Only Exception:** The only other language that can be added is **English (`en`)**. It is universally compatible.
3.  **Other Latin Languages are Disabled:** All other languages from the Latin group (such as French, German, Romanian) become **unavailable**.
4.  **Cyrillic – A Special Case:** If the active dominant group is Cyrillic, English (`en`) is not just allowed, but **mandatory**. It will be selected automatically and cannot be deselected.

### **Practical Examples:**

*   **Valid Combinations:**
    *   `['en', 'fr', 'ro']` (Only languages from the Latin group)
    *   `['ru', 'uk', 'en']` (Cyrillic group + mandatory English)
    *   `['ja', 'en']` (Japanese group + optional English)
    *   `['ar', 'en']` (Arabic group + optional English)

*   **INVALID Combinations (will lead to crash):**
    *   `['ru', 'ja']` (Two different dominant groups)
    *   `['ru', 'ro', 'en']` (Dominant group mixed with a Latin language other than English)
    *   `['ja', 'fr']` (Dominant group mixed with a Latin language other than English)

### **Conclusion**

To avoid errors, follow a simple rule: either use only Latin-script languages, OR choose **one** other language family and combine it **only** with English.