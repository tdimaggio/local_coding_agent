 ```typescript
import {BusinessRule} from '@servicenow/sdk/core';

export default BusinessRule('incident', (incident) => {
  if (incident.priority === '1') {
    incident.priority = '1'; // Ensure the value is set to P1 explicitly
  }
});
```
