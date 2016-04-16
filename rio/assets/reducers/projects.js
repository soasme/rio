export const NEW_PROJECT_SUCCESS = 'NEW_PROJECT_SUCCESS'

export const NEW_PROJECT_FAILURE = 'NEW_PROJECT_FAILURE'

export const projects = (state={}, action) => {

  switch (action.type) {
  case 'NEW_PROJECT_SUCCESS':
    var projects = new Map(state)
    projects.set(action.payload.id, action.payload)
    return projects

  case 'NEW_PROJECT_FAILURE':
    return state

  default:
    return state
  }
}
