import { createStore } from 'redux'
import rioReducer from '../reducers/index'

const store = createStore(rioReducer)

export default store
