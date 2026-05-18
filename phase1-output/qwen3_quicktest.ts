```typescript
import { BusinessRule } from "@servicenow/sdk/core";

BusinessRule({
  name: "Set Priority to 1 for P1 Incidents",
  description: "Sets priority field to 1 when a P1 incident is created",
  table: "incident",
  when: "before",
  condition: "current.priority == 1",
  action: (gr) => {
    gr.priority = 1;
  }
});
```
