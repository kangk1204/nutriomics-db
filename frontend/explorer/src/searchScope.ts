import type { Filters } from './types';

// Entity discovery must not imply that an item has the selected evidence type.
// The selected tier continues to control the evidence table and graph requests.
export const catalogSearchFilters = (filters: Filters): Filters => ({...filters,tier:'all'});
